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
    # unrelated value which happens to be sanitized in the same assignment.  A
    # conditional input (``str(e) if verbose else endpoint``) proves nothing
    # about the runtime value, so any branch in the arguments disqualifies it.
    args = [*value.args, *[kw.value for kw in value.keywords]]
    if any(
        isinstance(node, (ast.IfExp, ast.BoolOp, ast.NamedExpr))
        for arg in args
        for node in ast.walk(arg)
    ):
        return None
    if not any(
        isinstance(node, ast.Name) and node.id == exception_name
        for arg in args
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
        # ``case trace_text:`` / ``case [*trace_text]`` / ``case {**trace_text}``
        # bind through the pattern node, not through an ``ast.Name`` store.
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == name:
            return True
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name.split(".")[0]) == name for alias in node.names
        ):
            return True
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return True
    return False


def _is_type_projection(node: ast.AST, exception_name: str) -> bool:
    """True for a read of the exception that yields only its type or a bool.

    ``type(e)`` / ``e.__class__`` (and anything off them, e.g. ``.__name__``)
    and ``isinstance(e, ...)`` never format the message, so they cannot leak
    what a sanitizer would have redacted.
    """

    def is_exc(expr: ast.AST) -> bool:
        return isinstance(expr, ast.Name) and expr.id == exception_name

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "type" and len(node.args) == 1 and not node.keywords:
            return is_exc(node.args[0])
        if node.func.id == "isinstance" and len(node.args) == 2:
            return is_exc(node.args[0]) and not any(
                is_exc(inner) for inner in ast.walk(node.args[1])
            )
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__class__"
        and is_exc(node.value)
    )


_SAFE_EXCEPTION_METADATA_ATTRIBUTES = frozenset({"qualified_code", "status_code"})


def _is_safe_exception_metadata_projection(node: ast.AST, exception_name: str) -> bool:
    """True for stable typed-code/status fields, not exception message text."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr in _SAFE_EXCEPTION_METADATA_ATTRIBUTES
        and isinstance(node.value, ast.Name)
        and node.value.id == exception_name
    )


def call_logs_raw_exception(call: ast.Call, handler: ast.ExceptHandler) -> bool:
    """True when *call* also reads the caught exception outside a sanitizer.

    A sanitized alias only marks a redaction boundary when it is the sole route
    by which the exception reaches the log: ``logger.error("%s %s", safe, e)``
    still formats the raw exception, so there is no boundary to protect.
    Reads nested inside a recognised sanitizer call (``redact(e)``) and
    type-only projections (``type(e).__name__``) are fine. Stable metadata
    projections (``e.qualified_code`` / ``e.status_code``) are fine only beside
    a sanitizer of the caught exception itself: redacting an unrelated value
    (``redact(config)``) makes no boundary for the exception's own fields.
    Arbitrary exception fields such as ``e.message`` remain raw.
    """
    exception_name = handler.name
    if exception_name is None:
        return False
    args: list[ast.AST] = [*call.args, *[kw.value for kw in call.keywords]]
    sanitizes_exception = any(
        isinstance(node, ast.Call)
        and is_sanitizer_call(node)
        and any(
            isinstance(inner, ast.Name) and inner.id == exception_name
            for inner in ast.walk(node)
        )
        for arg in args
        for node in ast.walk(arg)
    )
    pending: list[ast.AST] = list(args)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Call) and is_sanitizer_call(node):
            continue
        if _is_type_projection(node, exception_name) or (
            sanitizes_exception
            and _is_safe_exception_metadata_projection(node, exception_name)
        ):
            continue
        if (
            isinstance(node, ast.Name)
            and node.id == exception_name
            and isinstance(node.ctx, ast.Load)
        ):
            return True
        pending.extend(ast.iter_child_nodes(node))
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


def is_sanitizer_call(expr: ast.expr) -> bool:
    """True when *expr* itself is a call to a recognised redaction helper.

    Stricter than "a sanitizer appears somewhere in *expr*": the value must be
    the helper's *output*.  ``redact(tb)`` and ``await utils.redact(tb)``
    count; ``redact("header") + raw_tb`` does not, because the raw traceback
    is concatenated past the redaction.  Used where a local is followed back
    to the expression that produced it (L004's sanitized-local exemption).
    """
    if isinstance(expr, ast.Await):
        expr = expr.value
    if not isinstance(expr, ast.Call):
        return False
    target = _leaf_name(expr.func)
    return target is not None and _name_is_sanitizer(target)


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

    When *handler* is supplied, no shape counts if the call *also* reads the
    caught exception outside a sanitizer (``logger.error("%s %s", redact(e),
    e)``): the raw exception is formatted anyway, so there is no redaction
    boundary to protect.
    """
    if handler is not None and call_logs_raw_exception(call, handler):
        return False
    for arg in [*call.args, *[kw.value for kw in call.keywords]]:
        for node in ast.walk(arg):
            if isinstance(node, ast.Call):
                target = _leaf_name(node.func)
                if target is not None and _name_is_sanitizer(target):
                    return True
            elif isinstance(node, ast.Name) and _name_is_sanitized_value(node.id):
                return True
    return handler is not None and _call_uses_sanitized_local_alias(call, handler)
