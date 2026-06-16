"""Checker — assembles rule mixins and drives the AST visitor dispatch."""

from __future__ import annotations

import ast

from conformance.suite.schema.findings import Finding

from ._directives import _IgnoreDirective
from .exception_chaining import ExceptionChainingMixin
from .security import SecurityMixin
from .silent_swallow import SilentSwallowMixin
from .untyped_raise import UntypedRaiseMixin


class Checker(
    SilentSwallowMixin,
    UntypedRaiseMixin,
    ExceptionChainingMixin,
    SecurityMixin,
    ast.NodeVisitor,
):
    """Walk a module AST and emit E-series findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        atlan_ioerror_imported: bool,
        legacy_aliases: frozenset[str] = frozenset(),
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._atlan_ioerror_imported = atlan_ioerror_imported
        self._legacy_aliases = legacy_aliases
        self._findings: list[Finding] = []
        # Context stacks — managed by visit_* methods
        self._function_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self._except_stack: list[ast.ExceptHandler] = []
        self._loop_stack: list[ast.For | ast.AsyncFor | ast.While] = []
        # Set True while generic_visit walks the children of a Raise node so
        # _check_p017_call can skip the inline Call (already covered by
        # _check_p017_raise on the outer Raise).
        self._in_raise_call: bool = False

    # ── Finding creation ──────────────────────────────────────────────────────

    def _add(self, rule_id: str, node: ast.AST, message: str) -> None:
        line: int = getattr(node, "lineno", 1)
        col: int = getattr(node, "col_offset", 0) + 1
        suppressed = False
        justification: str | None = None
        for check_line in (line, line - 1):
            if check_line in self._directives:
                d = self._directives[check_line]
                # Only honour a directive on the line *above* when that line is
                # comment-only.  A trailing inline directive on a code line
                # (e.g. ``do_it()  # conformance: ignore[E001]``) must NOT
                # absorb a finding on the following statement.
                if check_line == line - 1 and not d.comment_only:
                    continue
                if d.rule_ids is None or rule_id in d.rule_ids:
                    suppressed = True
                    justification = d.justification
                    break
        self._findings.append(
            Finding(
                rule_id=rule_id,
                file=self._filename,
                line=line,
                column=col,
                message=message,
                suppressed=suppressed,
                suppression_justification=justification,
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
        self._function_stack.append(node)
        self._check_p010_in_function(node)
        self.generic_visit(node)
        self._function_stack.pop()
        self._loop_stack = saved_loops
        self._except_stack = saved_excepts

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # type: ignore[override]
        saved_loops = self._loop_stack
        saved_excepts = self._except_stack
        self._loop_stack = []
        self._except_stack = []
        self._function_stack.append(node)  # type: ignore[arg-type]
        self._check_p010_in_function(node)  # type: ignore[arg-type]
        self.generic_visit(node)
        self._function_stack.pop()
        self._loop_stack = saved_loops
        self._except_stack = saved_excepts

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_p011(node)
        self.generic_visit(node)

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
        self._check_p001_p002_p006(node)
        self._check_p004(node)
        self._check_p005(node)
        self._check_p007(node)
        self._check_p008(node)
        self._check_p009(node)
        self._check_p014(node)
        self.generic_visit(node)
        self._except_stack.pop()

    def visit_Raise(self, node: ast.Raise) -> None:
        self._check_p012(node)
        self._check_p013(node)
        self._check_p015(node)
        self._check_p016(node)
        self._check_p017_raise(node)
        self._check_p018(node)
        self._in_raise_call = True
        self.generic_visit(node)
        self._in_raise_call = False

    def visit_Call(self, node: ast.Call) -> None:
        self._check_p003(node)
        self._check_p017_call(node)
        self.generic_visit(node)
