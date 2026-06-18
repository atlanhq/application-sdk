"""L008, L009 — log-performance and log-noise rules."""

from __future__ import annotations

import ast
from collections import deque

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._base import _MixinBase
from ._constants import EXPENSIVE_CALL_ATTRS, EXPENSIVE_CALL_NAMES
from ._helpers import get_logger_method

# ---------------------------------------------------------------------------
# L008 helpers
# ---------------------------------------------------------------------------


def _contains_expensive_call(node: ast.AST) -> bool:
    """True if *node* (or any non-scope descendant) is a call to an expensive function.

    Prunes Lambda / FunctionDef / AsyncFunctionDef so that a lambda *value*
    passed as a logger argument does not trigger L008 — the lambda body is not
    evaluated at the call site.
    """
    queue: deque[ast.AST] = deque([node])
    while queue:
        current = queue.popleft()
        if isinstance(current, ast.Call):
            func = current.func
            if isinstance(func, ast.Name) and func.id in EXPENSIVE_CALL_NAMES:
                return True
            if isinstance(func, ast.Attribute) and func.attr in EXPENSIVE_CALL_ATTRS:
                return True
        if not isinstance(current, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            queue.extend(ast.iter_child_nodes(current))
    return False


class PerformanceMixin(_MixinBase):
    """Rule methods for L008 (log-performance) and L009 (log-noise)."""

    # ── L008 UnguardedExpensiveDebug ──────────────────────────────────────────

    def _check_l008(self, node: ast.Call) -> None:
        """Flag expensive serialisation calls in ``logger.debug()`` arguments.

        Log-call arguments are evaluated eagerly regardless of whether the level
        is enabled.  An unguarded expensive serialisation inside logger.debug()
        runs on every call in production — invisible CPU/memory overhead that
        compounds on hot paths.

        Suppress with ``# conformance: ignore[L008] reason`` when the call is
        already inside an ``if logger.isEnabledFor(logging.DEBUG):`` guard (the
        checker cannot detect the guard reliably without parent tracking).
        """
        if get_logger_method(node) != "debug":
            return
        for arg in node.args[1:]:  # skip format string (args[0])
            if _contains_expensive_call(arg):
                self._add(
                    "L008",
                    node,
                    "Expensive computation in logger.debug() argument evaluates eagerly. "
                    "Guard with: if logger.isEnabledFor(logging.DEBUG): ...",
                )
                return
        for kw in node.keywords:
            if kw.arg == "exc_info":
                continue
            if _contains_expensive_call(kw.value):
                self._add(
                    "L008",
                    node,
                    "Expensive computation in logger.debug() argument evaluates eagerly. "
                    "Guard with: if logger.isEnabledFor(logging.DEBUG): ...",
                )
                return


# ---------------------------------------------------------------------------
# L009 — standalone visitor (needs statement-list context)
#
# Use WarnThenRaiseVisitor (not a Checker mixin) for any rule that requires
# comparing *adjacent statements* in a block; use a mixin when a single Call
# node carries enough information to emit a finding.
# ---------------------------------------------------------------------------


def _is_warn_or_error_log_stmt(stmt: ast.stmt) -> ast.Call | None:
    """Return the Call node if *stmt* is a ``logger.warning/error(...)`` expression."""
    if not isinstance(stmt, ast.Expr):
        return None
    call = stmt.value
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call):
        return None
    method = get_logger_method(call)
    if method in ("warning", "error"):
        return call
    return None


class WarnThenRaiseVisitor(ast.NodeVisitor):
    """Walk all statement blocks and emit L009 findings.

    L009 fires when a ``logger.warning/error(...)`` call appears within the
    three statements immediately preceding a ``raise`` in the same block.
    This creates duplicate log records (raise site + caller's handler),
    inflating error counts in dashboards.

    Acceptable: logging immediately before re-raise when the call *adds context*
    not available to the caller.  Use ``# conformance: ignore[L009] reason``
    with justification in that case.
    """

    def __init__(self, filename: str, directives: dict[int, _IgnoreDirective]) -> None:
        self._filename = filename
        self._directives = directives
        self._findings: list[Finding] = []

    def _scan_stmts(self, stmts: list[ast.stmt]) -> None:
        for i, stmt in enumerate(stmts):
            if not isinstance(stmt, ast.Raise):
                continue
            # Look back up to 3 statements for a warning/error log call
            for prev in reversed(stmts[max(0, i - 3) : i]):
                call = _is_warn_or_error_log_stmt(prev)
                if call is None:
                    continue
                method = (
                    call.func.attr
                    if isinstance(call.func, ast.Attribute)
                    else "warning"
                )  # type: ignore[union-attr]
                self._findings.append(
                    make_finding(
                        filename=self._filename,
                        rule_id="L009",
                        node=prev,
                        message=(
                            f"logger.{method}() immediately before raise — creates duplicate "
                            "log records (raise site + caller's handler), inflating error "
                            "counts. Acceptable only when adding context not available to "
                            "the caller; otherwise just re-raise."
                        ),
                        directives=self._directives,
                    )
                )
                break  # one finding per raise

    def visit_Module(self, node: ast.Module) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        self._scan_stmts(node.body)
        self._scan_stmts(node.orelse)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._scan_stmts(node.body)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._scan_stmts(node.body)
        self._scan_stmts(node.orelse)
        self._scan_stmts(node.finalbody)
        self.generic_visit(node)

    # Python 3.11+ try/except* blocks have the same structure as Try.
    # The type: ignore is needed because TryStar and Try are sibling AST nodes.
    visit_TryStar = visit_Try  # type: ignore[assignment]
