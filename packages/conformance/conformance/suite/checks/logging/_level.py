"""L006, L007, L017 — log-level rules."""

from __future__ import annotations

import ast

from ._base import _MixinBase
from ._helpers import get_logger_method, loop_is_bounded


def _following_statement(
    statement: ast.stmt, parents: dict[ast.AST, ast.AST]
) -> ast.stmt | None:
    """Return the statement immediately after *statement* in its containing suite."""
    suite = parents.get(statement)
    if suite is None:
        return None
    statements = next(
        (
            values
            for _, values in ast.iter_fields(suite)
            if isinstance(values, list) and any(value is statement for value in values)
        ),
        None,
    )
    if statements is None:
        return None
    index = next(index for index, value in enumerate(statements) if value is statement)
    if index + 1 >= len(statements):
        return None
    following = statements[index + 1]
    return following if isinstance(following, ast.stmt) else None


def _is_terminal_break_summary(
    node: ast.Call, loop: ast.For | ast.AsyncFor | ast.While
) -> bool:
    """True when a bare log statement immediately precedes a break from *loop*."""
    parents = {
        child: parent
        for parent in ast.walk(loop)
        for child in ast.iter_child_nodes(parent)
    }
    statement = parents.get(node)
    if not isinstance(statement, ast.Expr) or statement.value is not node:
        return False
    if not isinstance(_following_statement(statement, parents), ast.Break):
        return False

    child: ast.AST = statement
    ancestor: ast.AST | None = parents.get(statement)
    while ancestor is not None:
        if isinstance(ancestor, (ast.For, ast.AsyncFor, ast.While)):
            return ancestor is loop
        # A ``finally: continue`` overrides the break and re-runs the loop.
        if (
            isinstance(ancestor, (ast.Try, ast.TryStar))
            and not any(child is value for value in ancestor.finalbody)
            and _continues_enclosing_loop(ancestor.finalbody)
        ):
            return False
        child = ancestor
        ancestor = parents.get(ancestor)
    return False


def _is_terminal_return_summary(
    node: ast.Call, outermost: ast.For | ast.AsyncFor | ast.While
) -> bool:
    """True when a bare log is followed by a return that exits its function.

    The log may be the last statement in an ``if`` branch when the enclosing
    ``if`` is immediately followed by the return. A ``finally: continue``
    between the return and *outermost* (the function's outermost loop)
    overrides the return and repeats the log. A ``finally: break`` does too
    unless the loop it breaks is *outermost*: then no loop is left to repeat.
    """
    parents = {
        child: parent
        for parent in ast.walk(outermost)
        for child in ast.iter_child_nodes(parent)
    }
    statement = parents.get(node)
    if not isinstance(statement, ast.Expr) or statement.value is not node:
        return False

    current: ast.stmt = statement
    following = _following_statement(current, parents)
    while following is None:
        parent = parents.get(current)
        if not isinstance(parent, ast.If):
            return False
        current = parent
        following = _following_statement(current, parents)
    if not isinstance(following, ast.Return):
        return False

    child: ast.AST = following
    ancestor = parents.get(following)
    break_pending = False
    while ancestor is not None:
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        if isinstance(ancestor, (ast.For, ast.AsyncFor, ast.While)):
            # A ``finally: break`` exits this loop; an outer loop can re-log.
            if break_pending and ancestor is not outermost:
                return False
            break_pending = False
        if isinstance(ancestor, (ast.Try, ast.TryStar)) and not any(
            child is value for value in ancestor.finalbody
        ):
            if _continues_enclosing_loop(ancestor.finalbody):
                return False
            if _continues_enclosing_loop(ancestor.finalbody, include_break=True):
                break_pending = True
        child = ancestor
        ancestor = parents.get(ancestor)
    return True


def _continues_enclosing_loop(
    statements: list[ast.stmt], *, include_break: bool = False
) -> bool:
    """True when *statements* contain a ``continue`` for the enclosing loop.

    With *include_break*, a ``break`` for the enclosing loop also counts.
    """
    targets: tuple[type[ast.stmt], ...] = (
        (ast.Continue, ast.Break) if include_break else (ast.Continue,)
    )
    pending: list[ast.AST] = list(statements)
    while pending:
        current = pending.pop()
        if isinstance(current, targets):
            return True
        # A continue/break in a nested loop's body targets that loop; one in
        # its ``else`` still targets ours.  Nested scopes cannot reach ours.
        if isinstance(current, (ast.For, ast.AsyncFor, ast.While)):
            pending.extend(current.orelse)
        elif not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            pending.extend(ast.iter_child_nodes(current))
    return False


class LevelMixin(_MixinBase):
    """Rule methods for L006, L007, L017 (log-level category)."""

    # ── L006 InfoInTightLoop ──────────────────────────────────────────────────

    def _check_l006(self, node: ast.Call) -> None:
        """Flag ``logger.info()`` inside a loop that is not clearly bounded.

        Per-item INFO in a large loop emits O(N) records at the level operators
        monitor, drowning lifecycle signals in noise and inflating storage cost.
        INFO is for milestones; per-item progress belongs at DEBUG.

        Exempt: loops bounded to ≤ 10 iterations and terminal logs that end with
        a break (when outer loops do not repeat them) or an unconditional return.
        """
        if not self._loop_stack:
            return
        if get_logger_method(node, self._logging_module_names) != "info":
            return
        # Check if the innermost loop is clearly bounded
        innermost = self._loop_stack[-1]
        if isinstance(innermost, ast.While):
            # While loops are always flagged — bound is not statically obvious
            pass
        elif loop_is_bounded(innermost, self._bounded_loop_caps):  # type: ignore[arg-type]
            return
        enclosing_loops_are_bounded = all(
            isinstance(loop, (ast.For, ast.AsyncFor))
            and loop_is_bounded(loop, self._bounded_loop_caps)
            for loop in self._loop_stack[:-1]
        )
        if _is_terminal_return_summary(node, self._loop_stack[0]):
            return
        if enclosing_loops_are_bounded and _is_terminal_break_summary(node, innermost):
            return
        self._add(
            "L006",
            node,
            "logger.info() inside a loop — generates excessive log volume. "
            "Use DEBUG per-item and INFO for the loop summary.",
        )

    # ── L007 LoggerCriticalUsage ──────────────────────────────────────────────

    def _check_l007(self, node: ast.Call) -> None:
        """Flag ``logger.critical(...)``.

        ADR-0011 codifies exactly four levels (DEBUG/INFO/WARNING/ERROR).
        Fatal conditions are communicated through process exit codes and Temporal
        workflow failure — use ERROR (with exc_info=True) instead.
        """
        if get_logger_method(node, self._logging_module_names) != "critical":
            return
        self._add(
            "L007",
            node,
            "logger.critical() — CRITICAL is not a sanctioned level (ADR-0011). "
            "Use logger.error(..., exc_info=True) and let the failure propagate.",
        )

    # ── L017 LoggerExceptionUsage ─────────────────────────────────────────────

    def _check_l017(self, node: ast.Call) -> None:
        """Flag ``logger.exception(...)``.

        ADR-0011 restricts app logging to four levels with ``exc_info=True`` as
        the sanctioned way to attach a traceback.  ``logger.exception()`` reads
        ``sys.exc_info()`` implicitly — capturing nothing (or a stale exception)
        outside an active except block.

        Exempt: files that define ``AtlanLoggerAdapter`` or ``get_logger`` at
        module top-level (the logging infrastructure itself).  The exemption is
        whole-file, not method-scoped — see ``is_adapter_file`` in ``_helpers.py``.
        """
        if self._is_adapter_file:
            return
        if get_logger_method(node, self._logging_module_names) == "exception":
            self._add(
                "L017",
                node,
                "logger.exception() is not a sanctioned method (ADR-0011). "
                "Replace with logger.error(..., exc_info=True).",
            )
