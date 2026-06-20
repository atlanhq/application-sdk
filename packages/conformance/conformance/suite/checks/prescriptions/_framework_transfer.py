"""P008 FrameworkTransferInsideTask — ``self.upload()``/``self.download()`` in a ``@task``.

``App.upload()`` and ``App.download()`` are themselves framework tasks; nesting
them inside another ``@task``-decorated method runs a task within a task.  Data
that must move between tasks belongs on a ``FileReference`` contract field, not
in a nested transfer call.

Decorator provenance
--------------------
The check gates on import provenance: a decorator is only treated as the SDK
``task`` when the name is *not* explicitly imported from a non-SDK module.
``@celery.task``, ``@invoke.task``, etc. are excluded by tracking non-SDK
module names from ``ast.Import`` statements, and bare ``from celery import task``
style re-exports are excluded by tracking non-SDK ``from … import task`` names.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_TRANSFER_METHODS: frozenset[str] = frozenset({"upload", "download"})
_SDK_MODULE_PREFIX = "application_sdk"


def _collect_task_provenance(
    tree: ast.AST,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(non_sdk_task_names, non_sdk_module_names)`` for decorator gating.

    * ``non_sdk_task_names`` — local names bound to ``task`` from a non-SDK
      ``from … import task`` (e.g. ``from celery import task`` → ``{"task"}``).
    * ``non_sdk_module_names`` — local names of imported non-SDK top-level
      modules (e.g. ``import celery`` → ``{"celery"}``).

    P008 skips ``@name`` when ``name`` is in ``non_sdk_task_names``; skips
    ``@mod.task`` when ``mod`` is in ``non_sdk_module_names``.
    """
    non_sdk_task: set[str] = set()
    non_sdk_mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            is_sdk = module == _SDK_MODULE_PREFIX or module.startswith(
                _SDK_MODULE_PREFIX + "."
            )
            if not is_sdk:
                for alias in node.names:
                    if alias.name == "task":
                        non_sdk_task.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                is_sdk = name == _SDK_MODULE_PREFIX or name.startswith(
                    _SDK_MODULE_PREFIX + "."
                )
                if not is_sdk:
                    non_sdk_mods.add(alias.asname or name.split(".")[0])
    return frozenset(non_sdk_task), frozenset(non_sdk_mods)


def _is_task_decorator(
    dec: ast.expr,
    non_sdk_task_names: frozenset[str],
    non_sdk_module_names: frozenset[str],
) -> bool:
    """True if *dec* names the SDK ``task`` decorator (not a third-party one)."""
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        # @task — fire unless explicitly imported from a non-SDK module
        return dec.id == "task" and dec.id not in non_sdk_task_names
    if isinstance(dec, ast.Attribute):
        # @x.task — fire unless x is a known non-SDK module import
        return dec.attr == "task" and (
            not isinstance(dec.value, ast.Name)
            or dec.value.id not in non_sdk_module_names
        )
    return False


def _is_task_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    non_sdk_task_names: frozenset[str],
    non_sdk_module_names: frozenset[str],
) -> bool:
    return any(
        _is_task_decorator(dec, non_sdk_task_names, non_sdk_module_names)
        for dec in node.decorator_list
    )


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
    non_sdk_task_names, non_sdk_module_names = _collect_task_provenance(tree)
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_task_method(func, non_sdk_task_names, non_sdk_module_names):
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
