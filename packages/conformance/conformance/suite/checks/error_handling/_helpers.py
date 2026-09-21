"""Shared AST helper functions used across E-series rule modules."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import NamedTuple

from .._ast_common._exc_info import has_exc_info_traceback
from .._ast_common._sanitizers import expr_sanitizes_name
from ._constants import _BROAD_EXCEPT_TYPES, _LOG_METHODS, BUILTIN_RAISES


def _get_name(node: ast.expr | ast.AST | None) -> str | None:
    """Extract a simple name string from a Name, Attribute, or Subscript node."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _get_name(node.value)
    return None


def _raise_exc_name(exc: ast.expr) -> str | None:
    """Get the exception class name from a raise-expression.

    Handles both ``raise ValueError`` (Name) and ``raise ValueError(...)``
    (Call wrapping a Name or Attribute).
    """
    if isinstance(exc, ast.Call):
        return _get_name(exc.func)
    return _get_name(exc)


def _is_log_call_stmt(stmt: ast.stmt) -> bool:
    """True if *stmt* is a bare ``logger.<method>(...)`` expression."""
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS


def _any_logging_in(stmts: list[ast.stmt]) -> bool:
    """True if any logging call appears anywhere within *stmts*."""
    for stmt in stmts:
        if _is_log_call_stmt(stmt):
            return True
        for node in _iter_shallow(stmt):
            if isinstance(node, ast.stmt) and _is_log_call_stmt(node):
                return True
    return False


def _has_exc_info(call: ast.Call, exception_name: str | None = None) -> bool:
    """True if *call* passes an ``exc_info`` value that carries a traceback.

    Thin alias over :func:`_ast_common.has_exc_info_traceback`, shared with
    L004 — ``exc_info=<the except-as binding>`` attaches the same traceback as
    ``exc_info=True`` and must not read as a discarded stack trace here either.
    """
    return has_exc_info_traceback(call, exception_name)


def _body_is_only_pass(stmts: list[ast.stmt]) -> bool:
    """True if the body is exclusively Pass (ignoring docstring constants)."""
    real = [
        s
        for s in stmts
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) == 1 and isinstance(real[0], ast.Pass)


def _body_is_only_loop_control_no_logging(stmts: list[ast.stmt]) -> bool:
    """True if body only has continue/break/pass with no logging."""
    if _any_logging_in(stmts):
        return False
    real = [
        s
        for s in stmts
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) > 0 and all(
        isinstance(s, (ast.Continue, ast.Break, ast.Pass)) for s in real
    )


def _is_gather_call(call: ast.Call) -> bool:
    """True if *call* is ``asyncio.gather(...)`` or bare ``gather(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "gather"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
    if isinstance(func, ast.Name):
        return func.id == "gather"
    return False


def is_broad_suppress(node: ast.Call) -> bool:
    """True if *node* is ``contextlib.suppress(Exception|BaseException)``."""
    name = _get_name(node.func)
    if name != "suppress":
        return False
    for arg in node.args:
        if _get_name(arg) in _BROAD_EXCEPT_TYPES:
            return True
    return False


def is_builtin_raise(raise_node: ast.Raise) -> bool:
    """True if this raise targets a name in BUILTIN_RAISES."""
    if raise_node.exc is None:
        return False
    return _raise_exc_name(raise_node.exc) in BUILTIN_RAISES


def _get_decorator_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    names: set[str] = set()
    for dec in func.decorator_list:
        # @decorator → Name; @decorator("arg") → Call whose func is Name/Attribute
        if isinstance(dec, ast.Call):
            n = _get_name(dec.func)
        else:
            n = _get_name(dec)
        if n:
            names.add(n)
    return frozenset(names)


def _inherits_logging_filter(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        n = _get_name(base)
        if n == "Filter":
            return True
    return False


def _find_filter_method(
    cls: ast.ClassDef,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for item in cls.body:
        if (
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "filter"
        ):
            return item
    return None


def _filter_body_wrapped(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the filter() body is a single Try node (fully wrapped)."""
    real = [
        s
        for s in method.body
        if not (
            isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        )
    ]
    return len(real) == 1 and isinstance(real[0], ast.Try)


def _message_kw_has_exc_text(kw_value: ast.expr, exc_binding: str | None) -> bool:
    """True if a ``message=`` value embeds caught-exception text."""
    if exc_binding is None:
        return False
    # f-string containing the exception binding name
    if isinstance(kw_value, ast.JoinedStr):
        for part in ast.walk(kw_value):
            if isinstance(part, ast.FormattedValue):
                inner = part.value
                if isinstance(inner, ast.Name) and inner.id == exc_binding:
                    return True
                if (
                    isinstance(inner, ast.Call)
                    and _get_name(inner.func) in ("str", "repr")
                    and inner.args
                    and isinstance(inner.args[0], ast.Name)
                    and inner.args[0].id == exc_binding
                ):
                    return True
        return False
    # str(exc) / repr(exc) directly
    if isinstance(kw_value, ast.Call) and (
        _get_name(kw_value.func) in ("str", "repr")
        and kw_value.args
        and (
            isinstance(kw_value.args[0], ast.Name)
            and kw_value.args[0].id == exc_binding
        )
    ):
        return True
    # BinOp concat referencing the binding
    if isinstance(kw_value, ast.BinOp) and isinstance(kw_value.op, ast.Add):
        for node in ast.walk(kw_value):
            if isinstance(node, ast.Name) and node.id == exc_binding:
                return True
    return False


def _iter_shallow(root: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants of *root* without crossing nested function/class defs."""
    queue = list(ast.iter_child_nodes(root))
    while queue:
        node = queue.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            queue.extend(ast.iter_child_nodes(node))


def _raise_preserves_trace(stmt: ast.Raise) -> bool:
    """True when a ``raise`` keeps the original exception in the chain.

    ``raise`` (bare), ``raise X(...)`` (implicit ``__context__`` chaining), and
    ``raise X(...) from e`` (explicit ``__cause__``) all preserve the traceback.
    Only ``raise X(...) from None`` deliberately discards the context, so it is
    treated as trace-losing.
    """
    if isinstance(stmt.cause, ast.Constant) and stmt.cause.value is None:
        return False
    return True


class RedactionScope(NamedTuple):
    """Where a handler's caught exception survives in redacted form.

    ``exc_name`` is the handler's bound exception (``except Exception as exc``).
    ``redacted_at`` maps each ``raise`` statement in the handler (by node
    identity) to the local names that hold the caught exception in redacted form
    *at that statement* — see :func:`redaction_scope`.
    """

    exc_name: str
    redacted_at: dict[int, frozenset[str]]


def _references_any(expr: ast.expr, names: frozenset[str]) -> bool:
    """True when *expr* reads any of *names*."""
    if not names:
        return False
    return any(
        isinstance(node, ast.Name) and node.id in names for node in ast.walk(expr)
    )


def _rebound_names(target: ast.expr) -> set[str]:
    """Names *target* binds or unbinds — assignment, unpacking, ``del``, ``as``.

    Only ``Store``/``Del`` contexts count, so ``self`` in ``self.x = ...`` and
    ``buf`` in ``buf[0] = ...`` are left alone: those statements rebind an
    attribute or an element, not the name itself.
    """
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }


def _walrus_names(stmt: ast.stmt) -> set[str]:
    """Names bound by a ``:=`` anywhere in *stmt*, excluding nested scopes.

    A walrus can rebind a tracked local from inside an expression, where the
    statement-level dispatch below would not see it. It never *adds* one: the
    name is dropped either way, which is the conservative direction.
    """
    return {
        node.target.id
        for node in _iter_shallow(stmt)
        if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name)
    }


def _match_bound_names(pattern: ast.pattern) -> set[str]:
    """Names a ``match`` case pattern captures (``as`` targets, ``*rest``, ``**rest``)."""
    names: set[str] = set()
    for node in ast.walk(pattern):
        for field in ("name", "rest"):
            captured = getattr(node, field, None)
            if isinstance(captured, str):
                names.add(captured)
    return names


def _derives_from_cause(value: ast.expr, exc_name: str, live: set[str]) -> bool:
    """True when *value* carries the caught exception out in redacted form."""
    return expr_sanitizes_name(value, exc_name) or _references_any(
        value, frozenset(live)
    )


def _track_redacted(
    stmts: list[ast.stmt],
    exc_name: str,
    live: set[str],
    out: dict[int, frozenset[str]],
) -> set[str]:
    """Propagate redacted-cause locals through *stmts*, recording them at raises.

    Walks in source order and returns the names still holding the caught
    exception in redacted form once *stmts* has run.  A name is added when it is
    assigned from an expression that redacts the caught exception — directly, or
    by reading a name that is live at that point — and dropped the moment it is
    reassigned, augmented, deleted, unpacked, or rebound by a ``for`` target,
    ``with ... as``, ``:=``, import, ``def``/``class`` or ``match`` capture.

    Branch joins keep a name only when *every* path keeps it, and a loop body is
    iterated to a fixpoint, so a name that survives one pass but not the next is
    dropped.  Nested ``def``/``class`` bodies are not entered: assignments there
    belong to another scope.
    """
    live = set(live)
    for stmt in stmts:
        live -= _walrus_names(stmt)

        if isinstance(stmt, ast.Raise):
            out[id(stmt)] = frozenset(live)
            continue

        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            # The value is evaluated before the targets are bound, so `live` here
            # is still the pre-assignment set — `detail = sanitize(detail)` reads
            # the old `detail`.
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            # `detail: str` with no value is a bare annotation: it binds nothing.
            derives = stmt.value is not None and _derives_from_cause(
                stmt.value, exc_name, live
            )
            for target in targets:
                rebound = _rebound_names(target)
                # Only a plain `name = <expr>` carries the value across whole.
                # Unpacking (`a, b = ...`) hands each name a *part* of it, which
                # is not something the sanitizer's output can be assumed to be.
                if derives and isinstance(target, ast.Name):
                    live |= rebound
                else:
                    live -= rebound
            continue

        if isinstance(stmt, ast.AugAssign):
            # `detail += extra` folds in text that was never redacted.
            live -= _rebound_names(stmt.target)
            continue

        if isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                live -= _rebound_names(target)
            continue

        if isinstance(stmt, ast.If):
            taken = _track_redacted(stmt.body, exc_name, live, out)
            skipped = _track_redacted(stmt.orelse, exc_name, live, out)
            live = taken & skipped
            continue

        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            entry = set(live)
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                entry -= _rebound_names(stmt.target)
            # Shrink to a fixpoint: a name killed on a later iteration must not
            # survive because the first pass happened to keep it.
            while True:
                after = _track_redacted(stmt.body, exc_name, entry, out)
                narrowed = entry & after
                if narrowed == entry:
                    break
                entry = narrowed
            # Zero iterations leaves `live` untouched, so only names that hold on
            # both paths survive; `else` then runs on normal completion.
            live = _track_redacted(stmt.orelse, exc_name, entry & live, out)
            continue

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    live -= _rebound_names(item.optional_vars)
            live = _track_redacted(stmt.body, exc_name, live, out)
            continue

        if isinstance(stmt, (ast.Try, ast.TryStar)):
            body_live = _track_redacted(stmt.body, exc_name, live, out)
            paths = [body_live, _track_redacted(stmt.orelse, exc_name, body_live, out)]
            for handler in stmt.handlers:
                # A handler can start anywhere in the body, so it inherits the
                # entry set, minus its own bound exception name.
                entry = live - ({handler.name} if handler.name else set())
                paths.append(_track_redacted(handler.body, exc_name, entry, out))
            live = _track_redacted(
                stmt.finalbody, exc_name, set.intersection(*paths), out
            )
            continue

        if isinstance(stmt, ast.Match):
            paths = [set(live)]  # no case matched
            for case in stmt.cases:
                entry = live - _match_bound_names(case.pattern)
                paths.append(_track_redacted(case.body, exc_name, entry, out))
            live = set.intersection(*paths)
            continue

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            live.discard(stmt.name)  # binds the name; its body is another scope
            continue

        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                live.discard(alias.asname or alias.name.split(".")[0])
            continue

        if isinstance(stmt, (ast.Global, ast.Nonlocal)):
            live -= set(stmt.names)
            continue

    return live


def redaction_scope(handler: ast.ExceptHandler) -> RedactionScope | None:
    """Find where *handler*'s caught exception survives in redacted form.

    ``None`` when the handler does not bind the exception (``except Exception:``)
    — nothing is named, so nothing can be carried out in redacted form.

    The redacted text is often built up over several statements before the raise
    references it, so the locals are tracked rather than the raise expression
    alone::

        except Exception as exc:
            detail = sanitize_cause_repr(exc)
            message = f"credential rejected: {detail}"
            raise CredentialError(message) from None

    Tracking is flow-sensitive (see :func:`_track_redacted`): a local that is
    later overwritten, augmented or only redacted on one branch is not live at
    the raise that follows.
    """
    if handler.name is None:
        return None
    redacted_at: dict[int, frozenset[str]] = {}
    _track_redacted(handler.body, handler.name, set(), redacted_at)
    return RedactionScope(handler.name, redacted_at)


def _raise_redacts_cause(stmt: ast.Raise, scope: RedactionScope) -> bool:
    """True when a severed ``raise X(...) from None`` still carries its cause.

    ``from None`` exists precisely so a raw traceback cannot reach the sink —
    the frame may hold a resolved credential, and the SDK's loguru sinks format
    tracebacks with ``diagnose`` enabled.  Severing and *dropping* the cause is
    trace-loss; severing while the raised error's own arguments carry the cause
    through a sanitizer is not: the failure survives in redacted form, which is
    the property E004 is about.
    """
    if stmt.exc is None:
        return False
    if expr_sanitizes_name(stmt.exc, scope.exc_name):
        return True
    return _references_any(stmt.exc, scope.redacted_at.get(id(stmt), frozenset()))


def _raise_carries_cause(stmt: ast.Raise, scope: RedactionScope | None) -> bool:
    """True when *stmt* re-raises without losing the original failure."""
    if _raise_preserves_trace(stmt):
        return True
    return scope is not None and _raise_redacts_cause(stmt, scope)


def _body_always_raises(
    body: list[ast.stmt], scope: RedactionScope | None = None
) -> bool:
    """True when *body* is guaranteed to re-raise on every path (cause intact).

    A top-level ``raise`` makes everything after it unreachable, so the block
    always raises; an ``if``/``else`` whose branches both always-raise does too.
    A ``raise`` that only appears inside a conditional without a matching ``else``
    is not counted — the other path could still swallow. A ``raise ... from None``
    on the guaranteeing path disqualifies the body unless *scope* shows it carries
    the caught exception out in redacted form (see :func:`redaction_scope`).
    """
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return _raise_carries_cause(stmt, scope)
        if (
            isinstance(stmt, ast.If)
            and stmt.orelse
            and _body_always_raises(stmt.body, scope)
            and _body_always_raises(stmt.orelse, scope)
        ):
            return True
    return False


def _body_has_bypassing_exit(
    body: list[ast.stmt],
    *,
    loop_depth: int = 0,
    scope: RedactionScope | None = None,
) -> bool:
    """True when some path through *body* leaves the handler without a
    cause-preserving re-raise.

    ``_body_always_raises`` only proves that *a* guaranteeing raise is reached;
    it does not see a path that bypasses it. Bypasses:

    * ``return`` — exits the enclosing function, swallowing the exception.
    * ``raise ... from None`` — reaches a raise but discards the cause, unless
      *scope* shows it re-raises with the cause redacted rather than dropped.
    * ``break`` / ``continue`` — only when they belong to a loop *outside* the
      handler (``loop_depth == 0``): they escape to that outer loop and skip the
      trailing raise, so the exception is swallowed. A ``break``/``continue``
      controlling a loop *nested inside* the handler is ordinary loop control —
      the handler's trailing raise still runs — and must not disqualify.

    Recurses through compound statements (tracking loop nesting) but not into
    nested ``def``/``class`` bodies, where a ``return`` belongs to the inner
    scope. Conservative by design: over-firing merely re-introduces a
    suppression, whereas under-firing hides a real swallow (WARN tier).
    """
    for stmt in body:
        if isinstance(stmt, ast.Return):
            return True
        if isinstance(stmt, ast.Raise) and not _raise_carries_cause(stmt, scope):
            return True
        if isinstance(stmt, (ast.Break, ast.Continue)):
            if loop_depth == 0:
                return True
            continue  # loop control for a loop nested in the handler
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # different scope — its return/raise is not a handler exit
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            # break/continue inside the loop body target this loop, not the
            # handler; the loop's ``else`` runs after the loop, so it stays at
            # the handler's depth.
            if _body_has_bypassing_exit(
                stmt.body, loop_depth=loop_depth + 1, scope=scope
            ):
                return True
            if _body_has_bypassing_exit(
                stmt.orelse, loop_depth=loop_depth, scope=scope
            ):
                return True
            continue
        for block in _child_stmt_blocks(stmt):
            if _body_has_bypassing_exit(block, loop_depth=loop_depth, scope=scope):
                return True
    return False


def _child_stmt_blocks(stmt: ast.stmt) -> list[list[ast.stmt]]:
    """Statement blocks of a non-loop compound statement (``if``/``with``/``try``/
    ``match``), for depth-preserving recursion. Loops are handled separately so
    their nesting can be tracked."""
    blocks: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(stmt, field, None)
        if isinstance(value, list):
            blocks.append(value)
    # ast.TryStar (``except*``, PEP 654) is a separate node, not an ast.Try
    # subclass — include its handlers too, or a swallow inside an except* group
    # is invisible.
    if isinstance(stmt, (ast.Try, ast.TryStar)):
        blocks.extend(handler.body for handler in stmt.handlers)
    if isinstance(stmt, ast.Match):
        blocks.extend(case.body for case in stmt.cases)
    return blocks
