"""L004 — missing-traceback rule."""

from __future__ import annotations

import ast
from collections import deque

from .._ast_common._sanitizers import call_uses_sanitizer, expression_uses_sanitizer
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


def _sanitized_locals_before(
    handler: ast.ExceptHandler,
    call: ast.Call,
) -> set[str]:
    """Find direct local values in *handler* known sanitized before *call*.

    Only straight-line, single-name assignments establish a fact.  Rebinding
    invalidates it, and a compound statement or nested log position makes the
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

        assignment = _simple_local_assignment(statement)
        if assignment is not None:
            name, value = assignment
            if (value is not None and expression_uses_sanitizer(value)) or (
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
        else:
            for child in _walk_no_scope(statement):
                if isinstance(child, ast.NamedExpr):
                    sanitized.difference_update(_target_names(child.target))
    return sanitized


def _call_uses_sanitized_local(call: ast.Call, names: set[str]) -> bool:
    """True if one of *call*'s arguments uses a tracked sanitized local."""
    if not names:
        return False
    return any(
        isinstance(node, ast.Name) and node.id in names
        for arg in [*call.args, *[kw.value for kw in call.keywords]]
        for node in ast.walk(arg)
    )


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
        credentials (see _ast_common/_sanitizers.py).
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
            if call_uses_sanitizer(node) or _call_uses_sanitized_local(
                node, _sanitized_locals_before(handler, node)
            ):
                # Deliberate redaction boundary — exc_info would serialize the
                # raw exception past the sanitizer and can leak credentials.
                continue
            self._add(
                "L004",
                node,
                f"logger.{method}() in except block is missing exc_info=True — "
                "the stack trace is silently discarded. Add exc_info=True.",
            )
