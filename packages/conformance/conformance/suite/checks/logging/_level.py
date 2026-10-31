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


def _is_loop_else_summary(
    node: ast.Call, outermost: ast.For | ast.AsyncFor | ast.While
) -> bool:
    """True when a log is in the outermost active loop's ``else`` suite.

    That suite runs once on natural exhaustion. A nested loop or scope can make
    the call repeat or defer it, so those nodes disqualify the summary.
    """
    parents = {
        child: parent
        for parent in ast.walk(outermost)
        for child in ast.iter_child_nodes(parent)
    }
    child: ast.AST = node
    ancestor = parents.get(node)
    while ancestor is not None and ancestor is not outermost:
        if isinstance(
            ancestor,
            (
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            return False
        child = ancestor
        ancestor = parents.get(ancestor)
    return ancestor is outermost and any(
        child is statement for statement in outermost.orelse
    )


_LOOP_REDIRECTS = (
    ast.Break,
    ast.Continue,
    ast.Return,
    ast.Raise,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.TryStar,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


def _cannot_redirect_loop(statement: ast.stmt) -> bool:
    """True when *statement* can neither leave nor re-enter the enclosing loop.

    Plain expressions and assignments always fall through.  An ``if`` or
    ``with`` does too when nothing inside it breaks, continues, returns,
    raises, loops, catches or defines a scope.
    """
    if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return True
    if isinstance(statement, (ast.If, ast.With, ast.AsyncWith)):
        return not any(
            isinstance(inner, _LOOP_REDIRECTS) for inner in ast.walk(statement)
        )
    return False


def _following_effective_statement(
    statement: ast.stmt, parents: dict[ast.AST, ast.AST]
) -> ast.stmt | None:
    """The next sibling of *statement* that could redirect control flow.

    Siblings that :func:`_cannot_redirect_loop` are skipped.
    """
    following = _following_statement(statement, parents)
    while following is not None and _cannot_redirect_loop(following):
        following = _following_statement(following, parents)
    return following


def _is_terminal_return_summary(
    node: ast.Call, outermost: ast.For | ast.AsyncFor | ast.While
) -> bool:
    """True when a bare log is followed by a return that exits its function.

    Statements that cannot leave or re-enter the loop (expressions,
    assignments, and ``if``/``with`` blocks with no break, continue, return,
    raise, ``try``, nested loop or nested scope) may sit between the log and
    the return.  The log
    may be the last such statement in an ``if`` branch when the enclosing
    ``if`` is followed by the return in the same way. A ``finally: continue``
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
    following = _following_effective_statement(current, parents)
    while following is None:
        parent = parents.get(current)
        if not isinstance(parent, ast.If):
            return False
        current = parent
        following = _following_effective_statement(current, parents)
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


# ── L006 throttle recognition ────────────────────────────────────────────────
#
# A progress log gated by a modulo counter or an elapsed-time threshold fires
# once per N items or once per S seconds — a milestone, not per-item volume.
# Recognition is purely structural so it stays provable from the AST.

# Calls whose result is a clock reading: ``time.monotonic()``, ``time()``,
# ``datetime.now()``, ``loop.time()`` and the ``_ns`` variants.
_CLOCK_CALLS: frozenset[str] = frozenset(
    {
        "time",
        "time_ns",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "now",
        "utcnow",
    }
)

# ``_``-separated tokens that mark a name as holding a clock reading
# (``now``, ``last_log``, ``start_ts``, ``last_progress_time``).
_CLOCK_NAME_TOKENS: frozenset[str] = frozenset(
    {
        "now",
        "time",
        "ts",
        "timestamp",
        "last",
        "start",
        "started",
        "clock",
        "tick",
        "elapsed",
        "monotonic",
        "t0",
    }
)


def _is_clock_operand(node: ast.expr) -> bool:
    """True when *node* is a clock reading: a clock call or a time-ish name."""
    if isinstance(node, ast.Call):
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        return name in _CLOCK_CALLS
    name = (
        node.attr
        if isinstance(node, ast.Attribute)
        else node.id
        if isinstance(node, ast.Name)
        else None
    )
    if name is None:
        return False
    return any(token in _CLOCK_NAME_TOKENS for token in name.lower().split("_"))


def _reads_loop_item(node: ast.expr, item_names: frozenset[str]) -> bool:
    """True for a loop target (``ts``) or a field rooted at one (``q.start_time``)."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return isinstance(node, ast.Name) and node.id in item_names


def _reads_item_field(node: ast.expr, item_names: frozenset[str]) -> bool:
    """True for an attribute/subscript rooted at a loop target (``q.status``)."""
    return isinstance(node, (ast.Attribute, ast.Subscript)) and _reads_loop_item(
        node, item_names
    )


def _is_elapsed(node: ast.expr, item_names: frozenset[str]) -> bool:
    """True for ``a - b`` with a clock operand, or ``(a - b).total_seconds()``.

    The loop item or a field of it (``q.end_time - q.start_time``,
    ``now - ts`` for ``ts`` in timestamps) is a per-item filter, not a
    throttle, so an operand read from the item disqualifies it.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "total_seconds"
        and not node.args
    ):
        node = node.func.value
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and (_is_clock_operand(node.left) or _is_clock_operand(node.right))
        and not _reads_loop_item(node.left, item_names)
        and not _reads_loop_item(node.right, item_names)
    )


def _is_zero(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and type(node.value) is int  # excludes bool
        and node.value == 0
    )


def _is_throttle_compare(node: ast.expr, item_names: frozenset[str]) -> bool:
    """True for ``n % N == 0`` or an elapsed-time threshold comparison."""
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    left, op, right = node.left, node.ops[0], node.comparators[0]
    if isinstance(op, ast.Eq):
        for mod, other in ((left, right), (right, left)):
            if (
                isinstance(mod, ast.BinOp)
                and isinstance(mod.op, ast.Mod)
                and not isinstance(mod.left, (ast.Constant, ast.JoinedStr))
                and _is_zero(other)
            ):
                return True
        return False
    if isinstance(op, (ast.Gt, ast.GtE)):
        return _is_elapsed(left, item_names)
    if isinstance(op, (ast.Lt, ast.LtE)):
        return _is_elapsed(right, item_names)
    return False


def _is_one_shot_equality(node: ast.expr, item_names: frozenset[str]) -> bool:
    """True for an ``==`` that can hold on only one iteration (``done == total``).

    A comparison against a non-integer literal (``status == "failed"``) or one
    reading a field of the loop item (``item.status == ...``) is a per-item
    filter that can hold on every iteration, so it does not qualify.
    """
    if not (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
    ):
        return False
    for operand in (node.left, node.comparators[0]):
        if isinstance(operand, ast.Constant) and type(operand.value) is not int:
            return False
        if _reads_item_field(operand, item_names):
            return False
    return True


def _is_throttle_test(test: ast.expr, item_names: frozenset[str]) -> bool:
    """True when an ``if`` test only lets a throttled subset of iterations in.

    ``and`` narrows, so one throttle conjunct is enough.  ``or`` widens, so
    every disjunct must be a throttle or a one-shot ``==`` comparison (the
    ``completed == total`` final report, see :func:`_is_one_shot_equality`);
    any other disjunct can admit every item.
    """
    if _is_throttle_compare(test, item_names):
        return True
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.And):
            return any(_is_throttle_test(value, item_names) for value in test.values)
        return any(
            _is_throttle_test(value, item_names) for value in test.values
        ) and all(
            _is_throttle_test(value, item_names)
            or _is_one_shot_equality(value, item_names)
            for value in test.values
        )
    return False


def _is_throttled(node: ast.Call, loop: ast.For | ast.AsyncFor | ast.While) -> bool:
    """True when *node* sits in the body of a throttling ``if`` inside *loop*.

    Only ``if`` statements between the call and its innermost loop count: an
    ``if`` outside the loop does not limit how often the loop body runs, and
    the ``else`` branch of a throttle is the per-item path.
    """
    parents = {
        child: parent
        for parent in ast.walk(loop)
        for child in ast.iter_child_nodes(parent)
    }
    item_names = frozenset(
        target.id
        for target in (
            ast.walk(loop.target) if isinstance(loop, (ast.For, ast.AsyncFor)) else ()
        )
        if isinstance(target, ast.Name)
    )
    child: ast.AST = node
    ancestor = parents.get(node)
    while ancestor is not None and ancestor is not loop:
        if (
            isinstance(ancestor, ast.If)
            and any(child is statement for statement in ancestor.body)
            and _is_throttle_test(ancestor.test, item_names)
        ):
            return True
        child = ancestor
        ancestor = parents.get(ancestor)
    return False


class LevelMixin(_MixinBase):
    """Rule methods for L006, L007, L017 (log-level category)."""

    # ── L006 InfoInTightLoop ──────────────────────────────────────────────────

    def _check_l006(self, node: ast.Call) -> None:
        """Flag ``logger.info()`` inside a loop that is not clearly bounded.

        Per-item INFO in a large loop emits O(N) records at the level operators
        monitor, drowning lifecycle signals in noise and inflating storage cost.
        INFO is for milestones; per-item progress belongs at DEBUG.

        Exempt: loops bounded to ≤ 10 iterations, one-shot completion summaries
        in an outermost loop's ``else`` suite, terminal logs that end with a break
        (when outer loops do not repeat them) or an unconditional return, and
        throttled progress logs — the body of an ``if`` inside the innermost
        loop whose test is a modulo counter (``n % N == 0``) or an elapsed-time
        threshold (``now - last >= S``).
        """
        if not self._loop_stack:
            return
        if get_logger_method(node, self._logging_module_names) != "info":
            return
        if _is_loop_else_summary(node, self._loop_stack[0]):
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
        if _is_throttled(node, innermost):
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
