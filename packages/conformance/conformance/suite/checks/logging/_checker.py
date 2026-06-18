"""Checker — assembles L-series rule mixins and drives the AST visitor dispatch."""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._config import ConfigMixin
from ._format import FormatMixin
from ._helpers import detect_framework, is_adapter_file
from ._level import LevelMixin
from ._performance import PerformanceMixin
from ._print import PrintMixin
from ._security import SecurityMixin
from ._traceback import TracebackMixin


def _is_main_guard(test: ast.expr) -> bool:
    """True if *test* is the ``__name__ == '__main__'`` guard expression."""
    if not isinstance(test, ast.Compare):
        return False
    left = test.left
    if not (isinstance(left, ast.Name) and left.id == "__name__"):
        return False
    return any(
        isinstance(comp, ast.Constant) and comp.value == "__main__"
        for comp in test.comparators
    )


class Checker(
    FormatMixin,
    LevelMixin,
    TracebackMixin,
    SecurityMixin,
    PerformanceMixin,
    PrintMixin,
    ConfigMixin,
    ast.NodeVisitor,
):
    """Walk a module AST and emit L-series findings.

    Per-file checks only (L001–L009, L010–L015, L017–L020).
    L002 and L016 are cross-file and run in ``scan_all`` (``__init__.py``).
    """

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        framework: str,
        adapter_file: bool,
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._framework = framework
        self._is_adapter_file = adapter_file
        self._findings: list[Finding] = []
        # Context stacks — managed by visit_* methods
        self._loop_stack: list[ast.For | ast.AsyncFor | ast.While] = []
        self._except_stack: list[ast.ExceptHandler] = []
        # Track __main__ guard depth so L005 can exempt those blocks
        self._in_main_block: int = 0

    # ── Finding creation ──────────────────────────────────────────────────────

    def _add(self, rule_id: str, node: ast.AST, message: str) -> None:
        self._findings.append(
            make_finding(
                filename=self._filename,
                rule_id=rule_id,
                node=node,
                message=message,
                directives=self._directives,
            )
        )

    # ── Context management ────────────────────────────────────────────────────

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # type: ignore[override]
        # Reset loop/except context: handlers in a nested function are not
        # "inside" the outer function's loop or except block.
        saved_loops = self._loop_stack
        saved_excepts = self._except_stack
        self._loop_stack = []
        self._except_stack = []
        self.generic_visit(node)
        self._loop_stack = saved_loops
        self._except_stack = saved_excepts

    def visit_AsyncFunctionDef(  # type: ignore[override]
        self, node: ast.AsyncFunctionDef
    ) -> None:
        saved_loops = self._loop_stack
        saved_excepts = self._except_stack
        self._loop_stack = []
        self._except_stack = []
        self.generic_visit(node)
        self._loop_stack = saved_loops
        self._except_stack = saved_excepts

    def visit_For(self, node: ast.For) -> None:
        self._loop_stack.append(node)
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._loop_stack.append(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_While(self, node: ast.While) -> None:
        self._loop_stack.append(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._loop_stack.pop()

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._except_stack.append(node)
        self._check_l004_in_handler(node)
        self.generic_visit(node)
        self._except_stack.pop()

    def visit_If(self, node: ast.If) -> None:
        in_main = _is_main_guard(node.test)
        if in_main:
            self._in_main_block += 1
        self.generic_visit(node)
        if in_main:
            self._in_main_block -= 1

    # ── Call dispatch ─────────────────────────────────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:
        # Format rules
        self._check_l001(node)
        self._check_l003(node)
        self._check_l011(node)
        self._check_l014(node)
        self._check_l018(node)
        self._check_l020(node)
        # Level rules
        self._check_l006(node)
        self._check_l007(node)
        self._check_l017(node)
        # Security
        self._check_l010(node)
        # Performance
        self._check_l008(node)
        # Config / crash rules
        self._check_l012(node)
        self._check_l013(node)
        self._check_l015(node)
        # Print
        if not self._in_main_block:
            self._check_l005(node)
        self.generic_visit(node)

    # ── Expression dispatch (for bare calls) ──────────────────────────────────

    def visit_Expr(self, node: ast.Expr) -> None:
        """Check L019 — discarded bind() result (only meaningful on bare exprs)."""
        call = node.value
        if isinstance(call, ast.Await):
            call = call.value
        if isinstance(call, ast.Call):
            self._check_l019(call)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Module-level factory
# ---------------------------------------------------------------------------


def build_checker(
    text: str,
    file: str,
    directives: dict[int, _IgnoreDirective],
) -> tuple[Checker, ast.Module] | None:
    """Parse *text* and build a ready-to-run Checker.

    Returns ``None`` on ``SyntaxError`` so callers can skip the file cleanly.
    """
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return None
    framework = detect_framework(tree)
    adapter = is_adapter_file(tree)
    checker = Checker(
        filename=file,
        directives=directives,
        framework=framework,
        adapter_file=adapter,
    )
    return checker, tree
