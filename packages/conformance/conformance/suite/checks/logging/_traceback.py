"""L004 — missing-traceback rule."""

from __future__ import annotations

import ast
from collections import deque

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


class TracebackMixin(_MixinBase):
    """Rule method for L004 (missing-traceback category)."""

    # ── L004 ExceptBlockMissingExcInfoLog ─────────────────────────────────────

    def _check_l004_in_handler(self, handler: ast.ExceptHandler) -> None:
        """Flag warning/error calls inside an except block that lack exc_info=True.

        A log call without exc_info=True in an except block produces a message
        with no stack trace — the root cause is invisible.  Exempt: calls to
        ``logger.exception()`` (which implicitly sets exc_info) and any call
        that already carries ``exc_info=True``.
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
            if has_exc_info_true(node):
                continue
            self._add(
                "L004",
                node,
                f"logger.{method}() in except block is missing exc_info=True — "
                "the stack trace is silently discarded. Add exc_info=True.",
            )
