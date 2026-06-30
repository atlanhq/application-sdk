"""P026 GetattrOnTypedContractField — ``getattr(input, "f", default)`` on a typed contract.

P013/P014 require ``@entrypoint``/``@task`` boundaries to be typed ``Input``/``Output``
contracts.  This rule guards the *use* of that typed parameter: reading a declared
field via ``getattr(param, "field", default)`` defeats the contract — a renamed or
removed field silently yields the default (``""``/``None``) instead of raising
``AttributeError``, so the type is no longer load-bearing and contract drift goes
undetected.

Per-file: the decorator (provenance-gated) and the parameter annotation are both
local to the method.  Only the three-arg form (a *default* present) is flagged —
that is the silent-substitution hazard; a two-arg ``getattr(x, "f")`` still raises.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._decorator_provenance import (
    collect_import_provenance,
    is_entrypoint_decorator,
    is_task_decorator,
)
from ._typed_boundaries import _CLEARLY_UNTYPED, _annotation_terminal_name


def _typed_param_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names of params whose annotation looks like a typed contract (not a primitive)."""
    names: set[str] = set()
    args = func.args
    # Include keyword-only params (def fetch(self, *, input: X)) — the canonical
    # entrypoint/task form P013/P014 accept. Exclude **kwargs (args.kwarg): the
    # splat is a dict, never a typed contract.
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        if arg is None or arg.arg in ("self", "cls") or arg.annotation is None:
            continue
        # Unwrap Optional[X] / X | None / Annotated[X, ...] like P013/P014's helper,
        # so a PEP 604 union (input: FetchInput | None) is still seen as typed.
        terminal = _annotation_terminal_name(arg.annotation)
        # A real contract class — exclude clearly-untyped primitives/containers,
        # whose untyped boundary is already P013/P014's concern.
        if terminal is not None and terminal not in _CLEARLY_UNTYPED:
            names.add(arg.arg)
    return names


def _is_getattr_with_default(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 3
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    )


def check_p026(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P026 for ``getattr(<typed contract param>, "field", default)`` reads."""
    prov = collect_import_provenance(tree)
    findings: list[Finding] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(
            is_entrypoint_decorator(d, prov) or is_task_decorator(d, prov)
            for d in func.decorator_list
        ):
            continue
        typed = _typed_param_names(func)
        if not typed:
            continue
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and _is_getattr_with_default(node)
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in typed
            ):
                field = node.args[1].value  # type: ignore[attr-defined]
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P026",
                        node=node,
                        message=(
                            f"getattr({node.args[0].id}, {field!r}, <default>) reads a "
                            f"typed contract field with a fallback — a renamed/removed "
                            f"field silently yields the default instead of raising "
                            f"AttributeError, defeating the typed boundary. Use "
                            f"attribute access ({node.args[0].id}.{field})."
                        ),
                        directives=directives,
                    )
                )
    return findings
