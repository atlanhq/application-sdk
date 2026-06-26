"""P022 — UnawaitedCoroutine.

Flags a bare expression-statement call to a same-class ``async def`` method
(``self.<name>(...)`` with ``<name>`` an ``async def`` on the ``App`` subclass)
that is **not** awaited and **not** wrapped in ``create_task``/``gather``.  Such a
statement constructs a coroutine and immediately discards it — the work never
runs.  This is the most common "wrong async usage of the SDK" mistake: calling an
async SDK method like a sync function.

Scope is deliberately narrow (a bare ``self.<async-method>()`` statement inside an
``async def``) to stay false-positive-free: the target is provably a coroutine and
``await`` is a valid fix in the enclosing async context.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._workflow_methods import async_method_names

RULE_ID = "P022"

_HINT = (
    "This builds a coroutine and discards it — the call never runs. Add 'await' "
    "(or wrap it in asyncio.create_task/gather if you intend concurrency)."
)


class _Visitor(ast.NodeVisitor):
    """Walk the tree tracking the nearest enclosing function's async-ness."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        async_names: frozenset[str],
    ) -> None:
        self.filename = filename
        self.directives = directives
        self.async_names = async_names
        self._async_depth_stack: list[bool] = []
        self.findings: list[Finding] = []

    def _visit_func(self, node: ast.AST, is_async: bool) -> None:
        self._async_depth_stack.append(is_async)
        self.generic_visit(node)
        self._async_depth_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_func(node, is_async=True)

    def visit_Expr(self, node: ast.Expr) -> None:
        # A dropped coroutine is a bare Expr whose value is a Call (an awaited call
        # would be Expr(value=Await(...)), which never reaches here).
        call = node.value
        if (
            self._async_depth_stack
            and self._async_depth_stack[-1]
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            and call.func.attr in self.async_names
        ):
            self.findings.append(
                make_finding(
                    filename=self.filename,
                    rule_id=RULE_ID,
                    node=call,
                    message=(
                        f"Coroutine 'self.{call.func.attr}()' is never awaited. {_HINT}"
                    ),
                    directives=self.directives,
                )
            )
        self.generic_visit(node)


def check_p022(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit P022 findings for dropped (un-awaited) same-class coroutines."""
    async_names = async_method_names(tree)
    if not async_names:
        return []
    visitor = _Visitor(filename, directives, async_names)
    visitor.visit(tree)
    return visitor.findings
