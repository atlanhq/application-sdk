"""Detect failed checks without typed errors and report unresolved verdicts."""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry, is_preflightcheck_call, sdk_preflightcheck_locals

_P034 = "P034"
_MISSING = object()


def _resolved_kwargs(call: ast.Call, tree: ast.Module) -> dict[str, ast.expr] | None:
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    current: ast.AST = call
    while current in parents and not isinstance(
        current, (ast.FunctionDef, ast.AsyncFunctionDef)
    ):
        current = parents[current]
    bindings: dict[str, ast.expr] = {}
    if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for stmt in current.body:
            if stmt.lineno >= call.lineno:
                break
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                bindings[stmt.targets[0].id] = stmt.value
            else:
                touched = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)}
                for name in touched:
                    bindings.pop(name, None)

    def resolve(node: ast.expr, seen: frozenset[str] = frozenset()) -> ast.expr:
        if isinstance(node, ast.Name) and node.id in bindings and node.id not in seen:
            return resolve(bindings[node.id], seen | {node.id})
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            operand = resolve(node.operand, seen)
            if isinstance(operand, ast.Constant) and isinstance(operand.value, bool):
                return ast.Constant(value=not operand.value)
        return node

    result: dict[str, ast.expr] = {}
    for kw in call.keywords:
        value = resolve(kw.value)
        if kw.arg is not None:
            result[kw.arg] = value
        elif isinstance(value, ast.Dict):
            for key, item in zip(value.keys, value.values):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    return None
                result[key.value] = resolve(item)
        else:
            return None
    return result


def scan(reg: Registry) -> list[Finding]:
    findings: list[Finding] = []
    for src in reg.sources:
        local_names = set(sdk_preflightcheck_locals(src.tree))
        module_aliases = set(src.prov.sdk_contract_module_aliases)
        qualified_calls = set()
        for imported in ast.walk(src.tree):
            if (
                isinstance(imported, ast.ImportFrom)
                and imported.module == "application_sdk.handler"
            ):
                local_names.update(
                    alias.asname or alias.name
                    for alias in imported.names
                    if alias.name == "PreflightCheck"
                )
            if isinstance(imported, ast.Import):
                for alias in imported.names:
                    if alias.name == "application_sdk.handler":
                        if alias.asname:
                            module_aliases.add(alias.asname)
                        else:
                            qualified_calls.add(
                                "application_sdk.handler.PreflightCheck"
                            )
        if not local_names and not module_aliases and not qualified_calls:
            continue
        for node in ast.walk(src.tree):
            if not isinstance(node, ast.Call):
                continue
            if not (
                is_preflightcheck_call(
                    node.func, frozenset(local_names), frozenset(module_aliases)
                )
                or ast.unparse(node.func) in qualified_calls
            ):
                continue
            kwargs = _resolved_kwargs(node, src.tree)
            if kwargs is None:
                continue
            passed = kwargs.get("passed", ast.Constant(value=False))
            if isinstance(passed, ast.Constant) and passed.value is True:
                continue
            error = kwargs.get("error", _MISSING)
            error_typed = not (
                error is _MISSING
                or (isinstance(error, ast.Constant) and error.value is None)
            )
            if error_typed:
                continue
            definite_failure = (
                isinstance(passed, ast.Constant) and passed.value is False
            )
            message = (
                "PreflightCheck has passed=False, explicitly or by default, without a typed "
                "error. Supply error=<AppError>.to_failure_details() for failed checks."
                if definite_failure
                else "PreflightCheck has a dynamic passed value without a typed error; "
                "static analysis cannot establish whether a failed check is returned. "
                "Verify failure scenarios and supply typed errors on failing paths."
            )
            findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id=_P034 if definite_failure else "P065",
                    node=node,
                    message=message,
                    directives=src.directives,
                )
            )
    return findings
