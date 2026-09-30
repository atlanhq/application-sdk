"""Recognition of redaction/sanitizer helpers — deliberate no-traceback boundaries.

Several fleet apps deliberately omit ``exc_info=True`` on except-block logs and
instead format the exception through a redaction helper, because the raw
driver/API exception (and therefore its traceback) can embed credentials:
JDBC URLs carrying passwords, ``Authorization`` headers/HMACs, connection
properties, OAuth response bodies.  Typical shapes::

    logger.warning("close failed: %s", redact(e))
    logger.error("auth failed: %s\\n%s", sanitize_cause_repr(e), safe_traceback(e))

Demanding ``exc_info=True`` at such a site (L004/E005) is anti-security: the
separately-serialized traceback bypasses the redaction the code already
performs.  A production security review over the fleet remediation (FND-57)
confirmed this pattern in five connector repos.

This module is the shared detector both series use to exempt those sites.
Recognition is textual-by-name on purpose: the helpers live in each app, so
the checker cannot resolve them — but the naming is the documented convention.
"""

from __future__ import annotations

import ast

#: Substrings that mark a callable as a redaction helper.  Matched
#: case-insensitively against the call target's simple name (``redact_secrets``)
#: or attribute leaf (``utils.redact``).  ``sanitiz`` covers
#: sanitize/sanitizer/sanitised.
SANITIZER_NAME_WORDS: tuple[str, ...] = (
    "redact",
    "sanitiz",
    "scrub_secret",
    "safe_traceback",
    "mask_secret",
)

#: Substrings that mark a *variable* as holding pre-sanitised text.  Narrower
#: than the call-target words on purpose: a bare name passed as a log arg must
#: read as sanitised *output* (the redacted text/traceback built on a previous
#: line), not as any identifier that happens to contain "redact"/"sanitiz".
#: ``redact_count`` / ``redaction_enabled`` / ``sanitize_input`` are flags or
#: raw inputs, not redacted text — matching them would silently exempt a log
#: call that never redacted anything.
_SANITIZED_VALUE_WORDS: tuple[str, ...] = (
    "safe_traceback",
    "redacted",
    "sanitized",
    "sanitised",
    "masked",
    "scrubbed",
)


def _name_is_sanitizer(name: str) -> bool:
    """True if *name* looks like a redaction-helper *callable*."""
    lowered = name.lower()
    return any(word in lowered for word in SANITIZER_NAME_WORDS)


def _name_is_sanitized_value(name: str) -> bool:
    """True if a bare *variable* name marks it as already-sanitised text."""
    lowered = name.lower()
    return any(word in lowered for word in _SANITIZED_VALUE_WORDS)


def _leaf_name(expr: ast.expr) -> str | None:
    """Return the identifier a call target or variable resolves to, if simple."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def expr_sanitizes_name(expr: ast.expr, name: str) -> bool:
    """True when *expr* passes the variable *name* through a recognised sanitizer.

    Stricter than :func:`call_uses_sanitizer`: it is not enough for *some*
    sanitizer to appear in the expression — the redaction must be applied to
    the named variable.  ``sanitize_cause_repr(exc)`` and
    ``f"...{errors.redact(str(exc))}"`` both count for ``name="exc"``;
    ``redact(config)`` does not.

    Used where the exemption is a claim about a *specific* value surviving in
    redacted form (E004's severed-but-redacted re-raise), rather than about the
    handler having logged something through a redaction boundary.
    """
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        target = _leaf_name(node.func)
        if target is None or not _name_is_sanitizer(target):
            continue
        for arg in [*node.args, *[kw.value for kw in node.keywords]]:
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Name) and inner.id == name:
                    return True
    return False


def _source_position(node: ast.AST) -> tuple[int, int] | None:
    """Return an AST node's source position when it came from parsed source."""
    line = getattr(node, "lineno", None)
    column = getattr(node, "col_offset", None)
    if isinstance(line, int) and isinstance(column, int):
        return line, column
    return None


def _sanitized_assignment_name(stmt: ast.stmt, exception_name: str) -> str | None:
    """Return a simple local assigned the caught exception through a sanitizer."""
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        value = stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        target = stmt.target
        value = stmt.value
    else:
        return None

    if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
        return None
    sanitizer = _leaf_name(value.func)
    if sanitizer is None or not _name_is_sanitizer(sanitizer):
        return None

    # The sanitizer must wrap data from this handler's caught exception, not an
    # unrelated value which happens to be sanitized in the same assignment.
    if not any(
        isinstance(node, ast.Name) and node.id == exception_name
        for arg in [*value.args, *[kw.value for kw in value.keywords]]
        for node in ast.walk(arg)
    ):
        return None
    return target.id


def _statement_writes_name_before(
    stmt: ast.stmt, name: str, position: tuple[int, int]
) -> bool:
    """True if *stmt* rebinds *name* before a later log call."""
    for node in ast.walk(stmt):
        node_position = _source_position(node)
        if node_position is None or node_position >= position:
            continue
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            return True
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return True
    return False


def _call_uses_sanitized_local_alias(
    call: ast.Call, handler: ast.ExceptHandler
) -> bool:
    """Recognize a simple sanitizer-derived local passed directly to a log."""
    exception_name = handler.name
    call_position = _source_position(call)
    if exception_name is None or call_position is None:
        return False

    logged_names = {
        arg.id
        for arg in [*call.args, *[kw.value for kw in call.keywords]]
        if isinstance(arg, ast.Name)
    }
    if not logged_names:
        return False

    for index, stmt in enumerate(handler.body):
        stmt_position = _source_position(stmt)
        if stmt_position is None or stmt_position >= call_position:
            continue
        alias = _sanitized_assignment_name(stmt, exception_name)
        if alias is None or alias not in logged_names:
            continue

        # Reject an alias if any intervening statement can rebind it. The
        # source-position check also handles semicolon-separated statements and
        # writes inside a later conditional block without assuming that branch
        # executes.
        overwritten = any(
            _statement_writes_name_before(later, alias, call_position)
            for later in handler.body[index + 1 :]
            if (later_position := _source_position(later)) is not None
            and later_position < call_position
        )
        if not overwritten:
            return True
    return False


def call_uses_sanitizer(
    call: ast.Call, *, handler: ast.ExceptHandler | None = None
) -> bool:
    """True when any argument of *call* flows through a recognised sanitizer.

    Three shapes count:

    * a direct helper call among the arguments — ``redact(e)``,
      ``redact_secrets(str(e))``, ``errors.sanitize_cause_repr(e)``;
    * a variable argument whose *name* marks it as pre-sanitised text —
      ``safe_traceback`` in ``logger.error("…%s", safe_traceback)`` where the
      redacted text was built on a previous line; and
    * when *handler* is supplied, a bare local assigned directly from a
      recognised sanitizer applied to that handler's caught exception, provided
      no intervening write replaces the value.

    Bare names use the narrower ``_SANITIZED_VALUE_WORDS``
    (``redacted``/``sanitized``/``safe_traceback``/…) so a flag or counter like
    ``redact_count``/``redaction_enabled`` does not suppress the rule. Only the
    log call's own arguments are inspected — a sanitizer used elsewhere in the
    handler does not exempt an unrelated log call.
    """
    for arg in [*call.args, *[kw.value for kw in call.keywords]]:
        for node in ast.walk(arg):
            if isinstance(node, ast.Call):
                target = _leaf_name(node.func)
                if target is not None and _name_is_sanitizer(target):
                    return True
            elif isinstance(node, ast.Name) and _name_is_sanitized_value(node.id):
                return True
    return handler is not None and _call_uses_sanitized_local_alias(call, handler)
