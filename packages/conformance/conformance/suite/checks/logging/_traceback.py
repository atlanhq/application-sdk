"""L004 — missing-traceback rule."""

from __future__ import annotations

import ast
from collections import deque

from .._ast_common._sanitizers import (
    call_logs_raw_exception,
    call_uses_sanitizer,
    is_sanitizer_call,
)
from ._base import _MixinBase
from ._constants import LOG_METHODS_WITH_TRACEBACK
from ._helpers import has_exc_info_true, is_logger_call


def _walk_no_scope(node: ast.AST):
    """Yield child AST nodes, pruning at nested scope and handler boundaries.

    Stops descending into FunctionDef / AsyncFunctionDef / ClassDef / Lambda
    (nested scope) and into ExceptHandler (nested handler) so that log calls
    inside a nested except block are not double-counted: the Checker calls
    _check_l004_in_handler on each ExceptHandler separately, so walking into a
    nested handler here would attribute its calls to the outer handler.

    ast.Try is NOT pruned: the try-body and orelse/finalbody of an inner try
    statement are still in scope of the surrounding except handler and should
    fire L004 normally.  Only the ExceptHandler boundary is the correct prune
    point — each handler is its own _check_l004_in_handler invocation.
    """
    queue: deque[ast.AST] = deque(ast.iter_child_nodes(node))
    while queue:
        child = queue.popleft()
        yield child
        if not isinstance(
            child,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
                ast.ExceptHandler,
            ),
        ):
            queue.extend(ast.iter_child_nodes(child))


def _target_names(target: ast.expr) -> set[str]:
    """Return simple local names bound by an assignment target."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for element in target.elts for name in _target_names(element)}
    return set()


def _simple_local_assignment(
    statement: ast.stmt,
) -> tuple[str, ast.expr | None] | None:
    """Return a single local name/value assignment, if *statement* has one."""
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    ):
        return statement.targets[0].id, statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id, statement.value
    return None


def _assigned_local_names(statement: ast.stmt) -> set[str]:
    """Return local names rebound by a simple assignment statement."""
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        targets = [statement.target]
    elif isinstance(statement, ast.Delete):
        targets = statement.targets
    else:
        return set()
    return {name for target in targets for name in _target_names(target)}


def _walrus_targets(node: ast.AST) -> set[str]:
    """Return names bound by ``:=`` anywhere in *node* (same scope only)."""
    return {
        name
        for child in [node, *_walk_no_scope(node)]
        if isinstance(child, ast.NamedExpr)
        for name in _target_names(child.target)
    }


def _sanitized_locals_before(
    handler: ast.ExceptHandler,
    call: ast.Call,
) -> set[str]:
    """Find direct local values in *handler* known sanitized before *call*.

    Only straight-line, single-name assignments whose value *is* a sanitizer
    call (or an alias of a sanitized local) establish a fact.  Rebinding —
    including a ``:=`` anywhere in a statement — invalidates it, and a compound statement or nested log position makes the
    order/path ambiguous, so existing facts are dropped rather than guessed.
    """
    sanitized: set[str] = set()
    for statement in handler.body:
        if isinstance(statement, ast.Expr) and statement.value is call:
            break
        if any(child is call for child in _walk_no_scope(statement)):
            # The call is nested in a branch/expression; its reaching assignment
            # cannot be established by this straight-line analysis.
            sanitized.clear()
            break

        # A walrus binds before the enclosing assignment's own target, so drop
        # its targets first; the assignment below may then re-establish one.
        sanitized.difference_update(_walrus_targets(statement))

        assignment = _simple_local_assignment(statement)
        if assignment is not None:
            name, value = assignment
            if (value is not None and is_sanitizer_call(value)) or (
                isinstance(value, ast.Name) and value.id in sanitized
            ):
                sanitized.add(name)
            else:
                sanitized.discard(name)
            continue

        if isinstance(
            statement, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)
        ):
            sanitized.difference_update(_assigned_local_names(statement))
            continue

        if isinstance(statement, ast.Import):
            imported = {
                alias.asname or alias.name.split(".")[0] for alias in statement.names
            }
            sanitized.difference_update(imported)
        elif isinstance(statement, ast.ImportFrom):
            imported = {alias.asname or alias.name for alias in statement.names}
            sanitized.difference_update(imported)
        elif isinstance(
            statement,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.Try,
                ast.With,
                ast.AsyncWith,
                ast.Match,
            ),
        ):
            # Branches and loops can rebind locals conditionally; don't carry a
            # sanitized fact across a shape this narrow analysis cannot prove.
            sanitized.clear()
        elif isinstance(
            statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            sanitized.discard(statement.name)
    return sanitized


def _call_uses_sanitized_local(call: ast.Call, names: set[str]) -> bool:
    """True if one of *call*'s arguments reads a tracked sanitized local.

    Only loads count, and a name the call itself rebinds with ``:=`` is no
    longer the sanitized value: ``logger.error("%s", (tb := raw))`` logs raw.
    """
    args = [*call.args, *[kw.value for kw in call.keywords]]
    live = names - {name for arg in args for name in _walrus_targets(arg)}
    if not live:
        return False
    return any(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id in live
        for arg in args
        for node in ast.walk(arg)
    )


def _call_target_leaf(call: ast.Call) -> str | None:
    """Return a simple call target's name or attribute leaf."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _call_reads_name_without_branch(call: ast.Call, name: str) -> bool:
    """Whether *call* consumes *name* without a conditional or rebinding."""
    args = [*call.args, *[kw.value for kw in call.keywords]]
    if any(
        isinstance(node, (ast.IfExp, ast.BoolOp, ast.NamedExpr))
        for arg in args
        for node in ast.walk(arg)
    ):
        return False
    return any(
        isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == name
        for arg in args
        for node in ast.walk(arg)
    )


def _logs_sanitized_traceback(call: ast.Call, exception_name: str) -> bool:
    """True if a log argument carries this exception's redacted stack trace."""
    args = [*call.args, *[kw.value for kw in call.keywords]]
    for arg in args:
        for helper_call in ast.walk(arg):
            if not isinstance(helper_call, ast.Call):
                continue
            target = _call_target_leaf(helper_call)
            if target is not None and "safe_traceback" in target.lower():
                if _call_reads_name_without_branch(helper_call, exception_name):
                    return True
                continue
            if not is_sanitizer_call(helper_call):
                continue
            helper_args = [
                *helper_call.args,
                *[kw.value for kw in helper_call.keywords],
            ]
            if any(
                isinstance(node, (ast.IfExp, ast.BoolOp, ast.NamedExpr))
                for helper_arg in helper_args
                for node in ast.walk(helper_arg)
            ):
                continue
            if any(
                isinstance(formatted, ast.Call)
                and _call_target_leaf(formatted) == "format_exception"
                and _call_reads_name_without_branch(formatted, exception_name)
                for formatted in ast.walk(helper_call)
            ):
                return True
    return False


def _statement_rebinds_name(statement: ast.stmt, name: str) -> bool:
    """Whether *statement* may replace a handler's caught-exception local."""
    for node in _walk_no_scope(statement):
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            return True
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return True
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == name:
            return True
        if isinstance(node, ast.Import) and any(
            (alias.asname or alias.name.split(".")[0]) == name for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and any(
            (alias.asname or alias.name) == name for alias in node.names
        ):
            return True
    return False


def _source_position(node: ast.AST) -> tuple[int, int] | None:
    line = getattr(node, "lineno", None)
    column = getattr(node, "col_offset", None)
    if isinstance(line, int) and isinstance(column, int):
        return line, column
    return None


def _handler_logged_sanitized_traceback_before(
    handler: ast.ExceptHandler,
    call: ast.Call,
    logging_module_names: set[str],
) -> bool:
    """Whether an earlier top-level warning/error logged this handler's trace.

    Only a straight-line log of the same caught exception's sanitized traceback
    establishes the fact.  A cause-only sanitizer, another exception, a branch,
    or a nested handler cannot prove that this traceback was recorded first.
    """
    exception_name = handler.name
    call_position = _source_position(call)
    if exception_name is None or call_position is None:
        return False

    exception_rebound = False
    for statement in handler.body:
        statement_position = _source_position(statement)
        if statement_position is None:
            continue
        if statement_position >= call_position:
            break

        if (
            not exception_rebound
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
        ):
            prior_call = statement.value
            if (
                is_logger_call(prior_call, logging_module_names)
                and isinstance(prior_call.func, ast.Attribute)
                and prior_call.func.attr in LOG_METHODS_WITH_TRACEBACK
                and call_uses_sanitizer(prior_call, handler=handler)
                and _logs_sanitized_traceback(prior_call, exception_name)
            ):
                return True

        if _statement_rebinds_name(statement, exception_name):
            exception_rebound = True
    return False


class TracebackMixin(_MixinBase):
    """Rule method for L004 (missing-traceback category)."""

    # ── L004 ExceptBlockMissingExcInfoLog ─────────────────────────────────────

    def _check_l004_in_handler(self, handler: ast.ExceptHandler) -> None:
        """Flag warning/error calls inside an except block that lack exc_info=True.

        A log call without exc_info=True in an except block produces a message
        with no stack trace — the root cause is invisible.  Exempt: calls to
        ``logger.exception()`` (which implicitly sets exc_info), any call
        that already carries ``exc_info=True``, and calls whose arguments flow
        through a recognised redaction helper (``redact*``/``sanitiz*``/
        ``safe_traceback``/…), including a local value assigned from such a
        helper earlier in the handler.  Those mark a deliberate no-traceback
        boundary where ``exc_info`` would bypass the redaction and can leak
        credentials (see _ast_common/_sanitizers.py).  Once a warning/error call
        has logged this same handler's sanitized traceback, later calls that do
        not expose the raw exception do not need to repeat the trace.
        """
        for node in _walk_no_scope(handler):
            if not isinstance(node, ast.Call):
                continue
            if not is_logger_call(node, self._logging_module_names):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            method = func.attr
            if method == "exception":
                continue  # logger.exception() is handled by L017; not relevant here
            if method not in LOG_METHODS_WITH_TRACEBACK:
                continue
            if has_exc_info_true(node, handler.name):
                continue
            if call_uses_sanitizer(node, handler=handler) or (
                not call_logs_raw_exception(node, handler)
                and _call_uses_sanitized_local(
                    node, _sanitized_locals_before(handler, node)
                )
            ):
                # Deliberate redaction boundary — exc_info would serialize the
                # raw exception past the sanitizer and can leak credentials.
                continue
            if not call_logs_raw_exception(
                node, handler
            ) and _handler_logged_sanitized_traceback_before(
                handler, node, self._logging_module_names
            ):
                # The same redacted traceback is already in the stream; don't
                # repeat it on a later status log in this handler.
                continue
            self._add(
                "L004",
                node,
                f"logger.{method}() in except block is missing exc_info=True — "
                "the stack trace is silently discarded. Add exc_info=True.",
            )
