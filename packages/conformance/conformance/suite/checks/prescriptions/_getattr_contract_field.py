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


def _typed_param_annotations(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, ast.expr]:
    """Annotations of params that look like typed contracts, not primitives."""
    annotations: dict[str, ast.expr] = {}
    args = func.args
    # Include keyword-only params (def fetch(self, *, input: X)) — the canonical
    # entrypoint/task form P013/P014 accept. Exclude **kwargs (args.kwarg): the
    # splat is a dict, never a typed contract.
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        if arg.arg in ("self", "cls") or arg.annotation is None:
            continue
        # Unwrap Optional[X] / X | None / Annotated[X, ...] like P013/P014's helper,
        # so a PEP 604 union (input: FetchInput | None) is still seen as typed.
        terminal = _annotation_terminal_name(arg.annotation)
        # A real contract class — exclude clearly-untyped primitives/containers,
        # whose untyped boundary is already P013/P014's concern.
        if terminal is not None and terminal not in _CLEARLY_UNTYPED:
            annotations[arg.arg] = arg.annotation
    return annotations


def _is_getattr_with_default(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 3
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    )


def _repo_package_roots(by_name_all: dict[str, list[object]]) -> set[str]:
    """Top-level package names represented by the scanned class registry."""
    roots: set[str] = set()
    for records in by_name_all.values():
        for record in records:
            parts = record.file.replace("\\", "/").split("/")
            if parts[:1] == ["src"]:
                parts = parts[1:]
            if not parts:
                continue
            root = parts[0].removesuffix(".py")
            if root:
                roots.add(root)
    return roots


def _base_hierarchy_is_resolved(
    record: object,
    by_name_all: dict[str, list[object]],
    repo_roots: set[str],
    visiting: set[str] | None = None,
) -> bool:
    """Whether every base can be resolved from app classes or SDK registries."""
    from conformance.suite.checks._sdk_contract_mixins import (
        SDK_CONTRACT_BASE_FIELDS,
        SDK_TEMPLATE_CONTRACT_FIELDS,
    )

    if visiting is None:
        visiting = set()
    if record.name in visiting:
        return False
    visiting.add(record.name)
    sdk_names = set(SDK_CONTRACT_BASE_FIELDS) | set(SDK_TEMPLATE_CONTRACT_FIELDS)
    for base in record.bases:
        imported_root = record.non_sdk_bases.get(base)
        if (
            base in record.non_sdk_bases
            and imported_root is not None
            and imported_root not in repo_roots
            and imported_root != "application_sdk"
        ):
            return False
        if base in sdk_names and base not in record.non_sdk_bases:
            continue
        ancestors = by_name_all.get(base, [])
        if not ancestors:
            return False
        if not all(
            _base_hierarchy_is_resolved(
                ancestor, by_name_all, repo_roots, visiting.copy()
            )
            for ancestor in ancestors
        ):
            return False
    return True


def _declared_contract_fields(
    annotation: ast.expr,
    aliases: dict[str, str],
    aliases_by_file: dict[str, dict[str, str]],
    by_name: dict[str, object],
    by_name_all: dict[str, list[object]],
) -> tuple[set[str], bool] | None:
    """Return resolved fields and whether the complete base chain is known.

    ``None`` means the annotation did not resolve to a scanned or SDK contract,
    so P026 retains its conservative syntactic finding. For a known contract,
    only a field present in its resolved declaration set is within P026's scope.
    """
    from conformance.suite.checks._entrypoint_contract_fields import (
        resolve_contract_fields,
    )
    from conformance.suite.checks._sdk_contract_mixins import (
        SDK_CONTRACT_BASE_FIELDS,
        SDK_TEMPLATE_CONTRACT_FIELDS,
    )

    terminal = _annotation_terminal_name(annotation)
    if terminal is None:
        return None
    contract_name = aliases.get(terminal, terminal)
    records = by_name_all.get(contract_name, [])
    if not records and contract_name in by_name:
        records = [by_name[contract_name]]
    if records:
        field_names: set[str] = set()
        repo_roots = _repo_package_roots(by_name_all)
        resolved = True
        for record in records:
            class_aliases = aliases_by_file.get(record.file, {})
            field_names.update(
                field.name
                for field in resolve_contract_fields(
                    record.node,
                    class_aliases,
                    by_name,
                    by_name_all=by_name_all,
                )
            )
            resolved = resolved and _base_hierarchy_is_resolved(
                record, by_name_all, repo_roots
            )
        return field_names, resolved

    sdk_fields = SDK_CONTRACT_BASE_FIELDS.get(contract_name)
    if sdk_fields is None:
        sdk_fields = SDK_TEMPLATE_CONTRACT_FIELDS.get(contract_name)
    if sdk_fields is not None:
        return {field.name for field in sdk_fields}, True
    return None


def check_p026(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
    *,
    aliases: dict[str, str] | None = None,
    aliases_by_file: dict[str, dict[str, str]] | None = None,
    by_name: dict[str, object] | None = None,
    by_name_all: dict[str, list[object]] | None = None,
) -> list[Finding]:
    """Emit P026 only for getattr-with-default reads of declared contract fields."""
    from ._error_code_prefix import collect_classes, collect_import_aliases

    if isinstance(tree, ast.Module) and by_name is None:
        local_aliases = collect_import_aliases(tree)
        records = collect_classes(tree, filename, local_aliases)
        by_name = {record.name: record for record in records}
        by_name_all = {}
        for record in records:
            by_name_all.setdefault(record.name, []).append(record)
        aliases = local_aliases if aliases is None else aliases
        aliases_by_file = {filename: local_aliases}
    aliases = aliases or {}
    aliases_by_file = aliases_by_file or {}
    by_name = by_name or {}
    by_name_all = by_name_all or {}

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
        typed = _typed_param_annotations(func)
        if not typed:
            continue
        for node in ast.walk(func):
            if (
                not isinstance(node, ast.Call)
                or not _is_getattr_with_default(node)
                or not isinstance(node.args[0], ast.Name)
                or node.args[0].id not in typed
            ):
                continue
            field = node.args[1].value  # type: ignore[attr-defined]
            declaration = _declared_contract_fields(
                typed[node.args[0].id],
                aliases,
                aliases_by_file,
                by_name,
                by_name_all,
            )
            if declaration is not None:
                declared_fields, hierarchy_resolved = declaration
                if field not in declared_fields and hierarchy_resolved:
                    continue
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
                        f"attribute access ({node.args[0].id}.{field}) when "
                        f"{field!r} is declared on the contract and the param "
                        f"is not Optional; otherwise fix the contract first."
                    ),
                    directives=directives,
                )
            )
    return findings
