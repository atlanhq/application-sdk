"""P008 FrameworkTransferInTask — ``self.upload()``/``self.download()`` in a ``@task``.

``App.upload()`` and ``App.download()`` are themselves framework tasks; nesting
them inside another ``@task``-decorated method runs a task within a task.  Data
that must move between tasks belongs on a ``FileReference`` contract field, not
in a nested transfer call.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_TRANSFER_METHODS: frozenset[str] = frozenset({"upload", "download"})


def _is_task_decorator(dec: ast.expr) -> bool:
    """True if *dec* names ``task`` (``@task``, ``@task(...)``, ``@x.task`` ...)."""
    # @task(...) / @x.task(...)
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        return dec.id == "task"
    if isinstance(dec, ast.Attribute):
        return dec.attr == "task"
    return False


def _is_task_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_is_task_decorator(dec) for dec in node.decorator_list)


def _is_self_transfer_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _TRANSFER_METHODS
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _iter_own_scope(node: ast.AST) -> "list[ast.AST]":
    """Yield descendants of *node* without crossing into a nested function scope.

    Unlike :func:`ast.walk`, this does not descend into the body of any nested
    ``FunctionDef``/``AsyncFunctionDef``/``Lambda`` — calls there belong to the
    inner function, not the task being inspected.
    """
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        out.append(cur)
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(cur))
    return out


def check_p008(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P008 for ``self.upload()``/``self.download()`` inside a ``@task`` method."""
    findings: list[Finding] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_task_method(func):
            continue
        # Inspect the task body only — never descend into nested function
        # scopes, where a transfer call belongs to the inner function.
        for sub in _iter_own_scope(func):
            if _is_self_transfer_call(sub):
                assert isinstance(sub, ast.Call)
                assert isinstance(sub.func, ast.Attribute)
                attr = sub.func.attr
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P008",
                        node=sub,
                        message=(
                            f"self.{attr}() called inside a @task method — "
                            "App.upload()/download() are themselves framework "
                            "tasks and must be called from run(), not nested "
                            "inside another @task. For task-to-task data, use a "
                            "FileReference field on the contract instead."
                        ),
                        directives=directives,
                    )
                )
    return findings
