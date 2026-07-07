"""B005 NonAdditiveContractChange / B006 StaleContractLedger — AST-based checker.

Entrypoint-only scope: only Input/Output contracts referenced by ``@entrypoint``
methods or implicit ``App.run()`` are gated.  ``@task`` contracts are excluded.

The check uses the same cross-file class-registry machinery as P013/P014:
``collect_classes`` + ``resolve_ancestor`` for App-subclass detection, and
``is_entrypoint_decorator`` / ``is_task_decorator`` for decorator provenance.

Field extraction resolves the full inheritance hierarchy (``_resolve_contract_fields``):
in-repo base classes are resolved from their own AST body via ``by_name``; SDK-provided
contract bases (``Input``, ``Output``, ``PublishInputMixin``) that live outside the
scanned repo are resolved from the static registry in ``_sdk_contract_mixins``. This
means a field inherited from a base class or SDK mixin is tracked exactly like one
declared directly on the contract — no need to redeclare it just to stay ledger-protected.

Type normalization produces stable canonical strings that are consistent between
the ledger generator and the checker so small syntactic variations
(``Optional[str]`` vs ``str | None``) do not produce false positives.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
)
from conformance.suite.checks.prescriptions._contract_common import _unwrap_annotated
from conformance.suite.checks.prescriptions._decorator_provenance import (
    ImportProvenance,
    collect_import_provenance,
    is_entrypoint_decorator,
    is_task_decorator,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    _is_classvar_annotation,
    collect_classes,
    collect_import_aliases,
    resolve_ancestor,
)
from conformance.suite.checks.prescriptions._typed_boundaries import (
    _annotation_terminal_name,
    _get_non_self_params,
    _iter_class_body_methods,
)
from conformance.suite.schema.findings import Finding

from ._ledger_schema import ContractField, ContractLedger
from ._sdk_contract_mixins import SDK_CONTRACT_BASE_FIELDS

# ── Canonical type normalization ──────────────────────────────────────────────


def _is_none_node(node: ast.expr) -> bool:
    return (isinstance(node, ast.Constant) and node.value is None) or (
        isinstance(node, ast.Name) and node.id == "None"
    )


def _is_named(node: ast.expr, name: str) -> bool:
    """True if node is Name(id=name) or Attribute(attr=name) (e.g. typing.Optional)."""
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


_TYPING_LOWER: dict[str, str] = {
    "List": "list",
    "Dict": "dict",
    "Tuple": "tuple",
    "Set": "set",
    "FrozenSet": "frozenset",
    "Type": "type",
}


def _normalize_type_node(node: ast.expr) -> ast.expr:
    """Recursively normalize an annotation AST node to canonical form.

    Rules:
    - ``Annotated[X, ...]`` → normalize(X)
    - ``Optional[X]`` → normalize(X) | None
    - ``Union[X, Y, ...]`` → normalize(X) | normalize(Y) | ... (None on right)
    - ``None | X`` → normalize(X) | None  (canonical: non-None on left)
    - Recurse into subscript slices and union arms.
    - Lowercase typing aliases (List→list, Dict→dict, …) via AST rewrite.
    Handles both ``ast.Name`` and ``ast.Attribute`` forms (e.g. ``typing.Optional``).
    """
    # Strip Annotated[X, ...] → X (ast.Name form handled by _unwrap_annotated)
    unwrapped = _unwrap_annotated(node)
    if unwrapped is not node:
        return _normalize_type_node(unwrapped)
    # typing.Annotated[X, ...] — ast.Attribute form not handled by _unwrap_annotated
    if isinstance(node, ast.Subscript) and _is_named(node.value, "Annotated"):
        slice_ = node.slice
        inner = slice_.elts[0] if isinstance(slice_, ast.Tuple) else slice_
        return _normalize_type_node(inner)

    # Optional[X] → normalize(X) | None (handles ast.Name and ast.Attribute)
    if isinstance(node, ast.Subscript) and _is_named(node.value, "Optional"):
        inner = _normalize_type_node(node.slice)
        return ast.BinOp(left=inner, op=ast.BitOr(), right=ast.Constant(value=None))

    # Union[X, Y, ...] → X | Y | ... with None moved to the right
    if isinstance(node, ast.Subscript) and _is_named(node.value, "Union"):
        slice_ = node.slice
        elts = slice_.elts if isinstance(slice_, ast.Tuple) else [slice_]
        arms = [_normalize_type_node(e) for e in elts]
        result: ast.expr = arms[0]
        for arm in arms[1:]:
            result = ast.BinOp(left=result, op=ast.BitOr(), right=arm)
        return _normalize_type_node(result)  # re-normalize to canonicalize None

    # BinOp X | Y — recurse into arms; canonicalize None to the right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _normalize_type_node(node.left)
        right = _normalize_type_node(node.right)
        if _is_none_node(left):
            return ast.BinOp(left=right, op=ast.BitOr(), right=left)
        return ast.BinOp(left=left, op=ast.BitOr(), right=right)

    # Lowercase typing aliases — rewrite AST node before unparsing
    if isinstance(node, ast.Attribute) and node.attr in _TYPING_LOWER:
        return ast.Name(id=_TYPING_LOWER[node.attr], ctx=ast.Load())
    if isinstance(node, ast.Name) and node.id in _TYPING_LOWER:
        return ast.Name(id=_TYPING_LOWER[node.id], ctx=ast.Load())

    # Subscript — recurse into value and slice
    if isinstance(node, ast.Subscript):
        return ast.Subscript(
            value=_normalize_type_node(node.value),
            slice=_normalize_type_node(node.slice),
            ctx=node.ctx,
        )

    # Tuple slice — recurse into elements
    if isinstance(node, ast.Tuple):
        return ast.Tuple(
            elts=[_normalize_type_node(e) for e in node.elts],
            ctx=node.ctx,
        )

    return node


def _flatten_union(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _flatten_union(node.left) + _flatten_union(node.right)
    return [node]


def _canonical_type(node: ast.expr) -> str:
    """Return a normalized type string for stable ledger comparison."""
    normalized = _normalize_type_node(node)
    # Dedupe duplicate None arms (e.g. Optional[Optional[X]] → X | None not X | None | None)
    arms = _flatten_union(normalized)
    seen_none = False
    deduped: list[ast.expr] = []
    for arm in arms:
        if _is_none_node(arm):
            if not seen_none:
                seen_none = True
                deduped.append(arm)
        else:
            deduped.append(arm)
    if len(deduped) == 1:
        return ast.unparse(deduped[0])
    result: ast.expr = deduped[0]
    for arm in deduped[1:]:
        result = ast.BinOp(left=result, op=ast.BitOr(), right=arm)
    return ast.unparse(result)


# ── Field extraction ──────────────────────────────────────────────────────────


class _FieldInfo(NamedTuple):
    name: str
    canonical_type: str
    status: str  # "active" | "deprecated" | "sunset"
    node: ast.AnnAssign | None  # None for fields resolved via inheritance


def _field_status(ann_node: ast.AnnAssign) -> str:
    """Derive lifecycle status from the field's Pydantic Field() call.

    Recognizes (in priority order):
    - ``Field(..., deprecated=True, json_schema_extra={"x-lifecycle": "sunset"})`` → ``"sunset"``
    - ``Field(..., deprecated=True)`` → ``"deprecated"``
    - anything else → ``"active"``

    Sunset always also carries ``deprecated=True``; check ``x-lifecycle`` first.
    """
    value = ann_node.value
    if not isinstance(value, ast.Call):
        return "active"
    func = value.func
    is_field_call = (isinstance(func, ast.Name) and func.id == "Field") or (
        isinstance(func, ast.Attribute) and func.attr == "Field"
    )
    if not is_field_call:
        return "active"

    deprecated_true = False
    is_sunset = False

    for kw in value.keywords:
        if (
            kw.arg == "deprecated"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value  # True or a non-empty string message
        ):
            deprecated_true = True
        if kw.arg == "json_schema_extra" and isinstance(kw.value, ast.Dict):
            for k, v in zip(kw.value.keys, kw.value.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "x-lifecycle"
                    and isinstance(v, ast.Constant)
                    and v.value == "sunset"
                ):
                    is_sunset = True

    if is_sunset:
        return "sunset"
    if deprecated_true:
        return "deprecated"
    return "active"


def _iter_fields(classdef: ast.ClassDef) -> list[_FieldInfo]:
    """Return annotated field info declared directly on *classdef*'s own body.

    Does not resolve fields inherited from a base class or mixin — use
    :func:`_resolve_contract_fields` for that.
    """
    result = []
    for stmt in classdef.body:
        if not isinstance(stmt, ast.AnnAssign):
            continue
        if not isinstance(stmt.target, ast.Name):
            continue
        if _is_classvar_annotation(stmt.annotation):
            continue
        name = stmt.target.id
        if name.startswith("_"):
            continue
        result.append(
            _FieldInfo(
                name=name,
                canonical_type=_canonical_type(stmt.annotation),
                status=_field_status(stmt),
                node=stmt,
            )
        )
    return result


def _base_name(base: ast.expr) -> str | None:
    """Return the simple name of a base-class expression (``Name`` or ``Attribute``)."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _resolve_contract_fields(
    classdef: ast.ClassDef,
    aliases: dict[str, str],
    by_name: dict[str, ClassRecord],
) -> list[_FieldInfo]:
    """Return field info for *classdef*, resolved across its full base-class chain.

    In-repo base classes are resolved recursively from their own AST body (via
    *by_name*); SDK-provided contract bases not present in the scanned repo
    (``Input``, ``Output``, ``PublishInputMixin``) are resolved from the static
    registry in :mod:`_sdk_contract_mixins`. A field declared directly on a
    (sub)class always overrides a same-named field inherited from a base —
    mirrors Python's MRO. Cycle-safe: mirrors the ``visiting``-guarded pattern
    used by :func:`resolve_ancestor` in ``_error_code_prefix``.

    ``ClassRecord.bases`` (not ``rec.node.bases``) is used once recursion
    reaches a registered ancestor: ``collect_classes`` already de-aliases base
    names against *that ancestor's own file*, so re-deriving them here against
    *classdef*'s (possibly different) file's aliases would be wrong.

    Inherited fields always carry ``node=None``, even when the ancestor is
    defined in-repo: the ancestor's AST node belongs to a different file than
    the one being reported for *classdef*, so its ``lineno`` would be
    attributed to the wrong file if reused directly. Findings for inherited
    fields anchor on *classdef* itself instead (see call sites).
    """
    fields_by_name: dict[str, _FieldInfo] = {}

    def merge_ancestor(name: str, visiting: set[str]) -> None:
        if name in visiting:
            return  # cycle — treat as unknown, same as resolve_ancestor
        visiting.add(name)

        rec = by_name.get(name)
        if rec is not None:
            # Reversed so the leftmost grand-ancestor (highest MRO precedence)
            # is merged last and therefore wins the dict overwrite below.
            for base_name in reversed(rec.bases):
                merge_ancestor(base_name, visiting)
            for fi in _iter_fields(rec.node):
                fields_by_name[fi.name] = fi._replace(node=None)
        else:
            for sdk_field in SDK_CONTRACT_BASE_FIELDS.get(name, ()):
                fields_by_name[sdk_field.name] = _FieldInfo(
                    name=sdk_field.name,
                    canonical_type=sdk_field.canonical_type,
                    status=sdk_field.status,
                    node=None,
                )

        visiting.discard(name)

    visiting: set[str] = {classdef.name}
    for base in reversed(classdef.bases):
        bname = _base_name(base)
        if bname is not None:
            merge_ancestor(aliases.get(bname, bname), visiting)

    # Fields declared directly on classdef always win over inherited ones.
    for fi in _iter_fields(classdef):
        fields_by_name[fi.name] = fi

    return list(fields_by_name.values())


# ── Entrypoint contract discovery ─────────────────────────────────────────────


def _collect_entrypoint_contract_names(
    file_trees: dict[Path, ast.AST],
    by_name: dict[str, ClassRecord],
) -> frozenset[str]:
    """Return the class names of all entrypoint Input/Output contracts.

    Mirrors P013 boundary detection — collects contract class names instead of
    emitting findings.  ``@task`` contract names are excluded.
    """
    entrypoint_contracts: set[str] = set()
    app_cache: dict[str, bool | None] = {}

    for path, tree in file_trees.items():
        prov: ImportProvenance = collect_import_provenance(tree)
        aliases = collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}

        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue

            for func in _iter_class_body_methods(class_node):
                is_ep = False

                if any(
                    is_entrypoint_decorator(dec, prov) for dec in func.decorator_list
                ):
                    is_ep = True
                elif any(is_task_decorator(dec, prov) for dec in func.decorator_list):
                    continue  # @task — skip entirely

                elif func.name == "run" and isinstance(func, ast.AsyncFunctionDef):
                    for base in class_node.bases:
                        bname = _base_name(base)
                        if bname is None:
                            continue
                        bname = aliases.get(bname, bname)
                        if (
                            bname == "App"
                            or resolve_ancestor(bname, "App", by_name, app_cache, set())
                            is True
                        ):
                            is_ep = True
                            break

                if not is_ep:
                    continue

                non_self = _get_non_self_params(func)
                if non_self:
                    ann = non_self[0].annotation
                    if ann is not None:
                        name = _annotation_terminal_name(ann)
                        if name:
                            entrypoint_contracts.add(aliases.get(name, name))

                if func.returns is not None:
                    name = _annotation_terminal_name(func.returns)
                    if name:
                        entrypoint_contracts.add(aliases.get(name, name))

    return frozenset(entrypoint_contracts)


# ── Main scan function ────────────────────────────────────────────────────────


def scan_contract_compat(
    paths: list[Path],
    root: Path,
    ledger: ContractLedger,
) -> list[Finding]:
    """Emit B005/B006 for entrypoint contract backwards-compatibility violations.

    Two-pass:
    1. Parse every file; build the cross-file class registry (needed for
       App-subclass resolution, which determines whether ``run()`` is an
       implicit entrypoint).
    2. For each file, check every entrypoint-contract class against the ledger.
    """
    # Pass 1: parse + build class registry
    file_trees: dict[Path, ast.AST] = {}
    file_directives: dict[Path, dict[int, _IgnoreDirective]] = {}
    file_aliases: dict[Path, dict[str, str]] = {}
    by_name: dict[str, ClassRecord] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        file_trees[path] = tree
        file_directives[path] = _parse_directives(text)

        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        aliases = collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}
        file_aliases[path] = aliases
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)

    entrypoint_names = _collect_entrypoint_contract_names(file_trees, by_name)

    if not entrypoint_names:
        return []

    # Pre-index the ledger for O(1) lookups
    ledger_by_key: dict[tuple[str, str], ContractField] = {
        (f.contract, f.field): f for f in ledger.fields
    }
    ledger_by_contract: dict[str, list[ContractField]] = {}
    for f in ledger.fields:
        ledger_by_contract.setdefault(f.contract, []).append(f)

    findings: list[Finding] = []

    # Pass 2: per-file contract checks
    for path, tree in file_trees.items():
        directives = file_directives.get(path, {})
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue
            if class_node.name not in entrypoint_names:
                continue

            aliases = file_aliases.get(path, {})
            live_fields = _resolve_contract_fields(class_node, aliases, by_name)
            live_by_name = {f.name: f for f in live_fields}

            # B005: every ledger field must still exist with its recorded type
            for lf in ledger_by_contract.get(class_node.name, []):
                live = live_by_name.get(lf.field)
                if live is None:
                    findings.append(
                        make_finding(
                            filename=rel,
                            rule_id="B005",
                            node=class_node,
                            message=(
                                f"Contract field '{class_node.name}.{lf.field}' "
                                f"(ledger type: '{lf.type}', status: '{lf.status}') "
                                "was removed from the contract. Entrypoint contract "
                                "fields are permanent — mark it 'deprecated' or "
                                "'sunset' and keep it. Removal breaks every consumer "
                                "that already serializes this field. "
                                "Suppress with '# conformance: ignore[B005] <reason>' "
                                "only if this contract has no deployed consumers."
                            ),
                            directives=directives,
                        )
                    )
                elif live.canonical_type != lf.type:
                    inherited_note = (
                        " (inherited from a base class or mixin)"
                        if live.node is None
                        else ""
                    )
                    findings.append(
                        make_finding(
                            filename=rel,
                            rule_id="B005",
                            node=live.node or class_node,
                            message=(
                                f"Contract field '{class_node.name}.{live.name}'"
                                f"{inherited_note} type changed from '{lf.type}' "
                                f"(ledger) to '{live.canonical_type}' (current). "
                                "Type changes break serialized payloads. Revert to "
                                f"'{lf.type}', or deprecate/sunset this field and add "
                                "a new one with the new type. "
                                "Suppress with '# conformance: ignore[B005] <reason>' "
                                "only if this contract has no deployed consumers."
                            ),
                            directives=directives,
                        )
                    )

            # B006: every live field must be recorded in the ledger
            for fi in live_fields:
                if (class_node.name, fi.name) not in ledger_by_key:
                    inherited_note = (
                        " (inherited from a base class or mixin)"
                        if fi.node is None
                        else ""
                    )
                    findings.append(
                        make_finding(
                            filename=rel,
                            rule_id="B006",
                            node=fi.node or class_node,
                            message=(
                                f"Contract field '{class_node.name}.{fi.name}'"
                                f"{inherited_note} is not recorded in the contract "
                                "ledger (contract_schema.lock.json). Run "
                                "'uv run atlan-application-sdk-conformance "
                                "gen-contract-ledger' (writes contract_schema.lock.json "
                                "in the repo root) and commit that file in the same PR. "
                                "The generator is append-only — it "
                                "can never launder a removal."
                            ),
                            directives=directives,
                        )
                    )

    return findings
