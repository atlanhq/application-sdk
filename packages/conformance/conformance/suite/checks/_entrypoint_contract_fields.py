"""Entrypoint contract discovery and field extraction — shared across rule series.

Neutral home for logic that isn't owned by any single rule series: today it
backs the B-series B005/B006 contract-compat checker and the contract-ledger
generator; the K-series (contract-toolkit) is expected to need the same
entrypoint-contract and field-resolution primitives, which is why this lives
at ``suite/checks/`` top level rather than inside ``deprecation/``.

The check uses the same cross-file class-registry machinery as P013/P014:
``collect_classes`` + ``resolve_ancestor`` for App-subclass detection, and
``is_entrypoint_decorator`` / ``is_task_decorator`` for decorator provenance.

Field extraction resolves the full inheritance hierarchy (``resolve_contract_fields``):
in-repo base classes are resolved from their own AST body via ``by_name``; SDK-provided
contract bases (``Input``, ``Output``, ``PublishInputMixin``) and SDK template
contract bases (such as ``ExtractionOutput``) that live outside the scanned repo
are resolved from the static registries in ``_sdk_contract_mixins``. This
means a field inherited from a base class or SDK mixin is tracked exactly like one
declared directly on the contract — no need to redeclare it just to stay ledger-protected.

Type normalization produces stable canonical strings that are consistent between
producers and readers of this data so small syntactic variations
(``Optional[str]`` vs ``str | None``) do not produce false positives.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

from conformance.suite.checks._sdk_contract_mixins import (
    SDK_CONTRACT_BASE_FIELDS,
    SDK_MODEL_BACKED_ARTIFACT_FIELDS,
    SDK_TEMPLATE_CONTRACT_FIELDS,
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
    collect_import_aliases,
    resolve_ancestor,
)
from conformance.suite.checks.prescriptions._typed_boundaries import (
    _annotation_terminal_name,
    _get_non_self_params,
    _iter_class_body_methods,
)

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


#: Matches the SDK's ``AssetArtifact`` marker in a field's **raw** annotation.
#:
#: Deliberately not read off ``canonical_type``: ``_normalize_type_node`` strips
#: ``Annotated[...]``, which is the only place the marker ever appears, so the
#: canonical string can never carry it. Matching the source text means an import
#: alias is not recognised — the same false-negative direction
#: ``_FILE_REFERENCE_RE`` already accepts, and the safe one here: an unrecognised
#: marker means the field is *reported*, never silently waved through.
_ASSET_ARTIFACT_RE = re.compile(r"\bAssetArtifact\b")


def _annotation_declares_model(annotation: ast.expr) -> bool:
    """Whether *annotation* carries the ``AssetArtifact`` marker.

    The marker says this ``FileReference`` field's declaration *is* an executable
    model — ``pyatlan_v9``'s ``Asset`` — rather than a hand-authored field map, so
    there is nothing for the app to write down and nothing for a rule to demand
    (FND-1863).
    """
    try:
        return bool(_ASSET_ARTIFACT_RE.search(ast.unparse(annotation)))
    except (AttributeError, ValueError):  # pragma: no cover — defensive
        return False


class _FieldInfo(NamedTuple):
    name: str
    canonical_type: str
    status: str  # "active" | "deprecated" | "sunset"
    node: ast.AnnAssign | None  # None for fields resolved via inheritance
    #: Whether this field's declaration is a typed model rather than a field map.
    #:
    #: **Provenance, carried rather than re-derived.** The marker lives in
    #: ``Annotated`` metadata, which canonicalisation strips and which an
    #: inherited field's ``node=None`` puts out of a caller's reach entirely. A
    #: caller left to answer "is this field model-declared?" for itself can only
    #: fall back to matching the field *name*, and a name is not a marker: it
    #: waves through any same-named field nothing declares, while reporting a
    #: marked field the app factored onto a base of its own. Resolving it here,
    #: where the annotation is still in hand, is what makes both answers right.
    #:
    #: Trailing with a default so every existing positional construction of this
    #: tuple keeps working.
    model_declared: bool = False


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
    :func:`resolve_contract_fields` for that.
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
                model_declared=_annotation_declares_model(stmt.annotation),
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


def _keep_model_declared(new: _FieldInfo, previous: _FieldInfo | None) -> _FieldInfo:
    """Carry ``model_declared`` forward when a redeclaration would drop it.

    Every other attribute of a field is overridden by the most derived
    declaration, mirroring Python's MRO. The marker is not, and it has to not be,
    because the SDK reader it mirrors behaves the same way: Pydantic rebuilds a
    redeclared field's metadata tuple from the new annotation, so a subclass that
    narrows a type or attaches its own ``Field(...)`` loses the marker from its
    own ``model_fields`` — which is why
    :func:`application_sdk.contracts.types.asset_artifact_marker` resolves across
    the MRO rather than reading one class.

    If this rule took the redeclaration at face value it would report a field the
    SDK exempts, on a boundary the interceptor is model-validating in full. The
    two answers have to agree, and the marker asserts a fact about the *bytes* —
    narrowing an annotation does not change what wrote them.
    """
    if previous is not None and previous.model_declared and not new.model_declared:
        return new._replace(model_declared=True)
    return new


def resolve_contract_fields(
    classdef: ast.ClassDef,
    aliases: dict[str, str],
    by_name: dict[str, ClassRecord],
    *,
    by_name_all: dict[str, list[ClassRecord]] | None = None,
) -> list[_FieldInfo]:
    """Return field info for *classdef*, resolved across its full base-class chain.

    In-repo base classes are resolved recursively from their own AST body (via
    *by_name*); SDK-provided contract bases not present in the scanned repo
    (``Input``, ``Output``, ``PublishInputMixin``) and SDK template contracts
    (such as ``ExtractionOutput``) are resolved from the static registries in
    :mod:`_sdk_contract_mixins`. A field declared directly on a
    (sub)class always overrides a same-named field inherited from a base —
    mirrors Python's MRO. Cycle-safe: mirrors the ``visiting``-guarded pattern
    used by :func:`resolve_ancestor` in ``_error_code_prefix``.

    Each ancestor contributes its fields at most once, via the first (highest
    MRO precedence) path that reaches it — tracked by *merged*, separate from
    the per-branch cycle-detection *visiting* set. Without this, a shared
    ancestor in diamond-shaped multiple inheritance (``D(B, C)`` where both
    ``B`` and ``C`` derive from ``A``) would be re-merged once per path,
    re-applying its fields and clobbering an override a higher-precedence
    sibling already established for the same field name.

    *by_name_all* is optional and opt-in: when a base-class NAME is declared
    more than once in the scanned repo (two entrypoints that both declare
    ``AppInputContract``, say), ``by_name`` keeps only the first and the
    resolved field set silently becomes whichever declaration happened to be
    parsed first. Passing the full multimap makes such an ambiguous ancestor
    contribute the UNION of its declarations instead — the honest reading when
    a bare class name cannot identify one of them. Callers that need today's
    first-wins behaviour simply omit it.

    ``ClassRecord.bases`` (not ``rec.node.bases``) is used once recursion
    reaches a registered ancestor: ``collect_classes`` already de-aliases base
    names against *that ancestor's own file*, so re-deriving them here against
    *classdef*'s (possibly different) file's aliases would be wrong.

    Inherited fields always carry ``node=None``, even when the ancestor is
    defined in-repo: the ancestor's AST node belongs to a different file than
    the one being reported for *classdef*, so its ``lineno`` would be
    attributed to the wrong file if reused directly. Callers should anchor
    findings for inherited fields on *classdef* itself instead.
    """
    fields_by_name: dict[str, _FieldInfo] = {}
    merged: set[str] = set()

    def merge_ancestor(name: str, visiting: set[str]) -> None:
        if name in merged:
            return  # already contributed via a higher-precedence path
        if name in visiting:
            return  # cycle — treat as unknown, same as resolve_ancestor
        visiting.add(name)

        recs = (by_name_all or {}).get(name)
        if not recs:
            rec_one = by_name.get(name)
            recs = [rec_one] if rec_one is not None else []
        if recs:
            for rec in recs:
                # Reversed so the leftmost grand-ancestor (highest MRO
                # precedence) is merged last and therefore wins the dict
                # overwrite below.
                for base_name in reversed(rec.bases):
                    merge_ancestor(base_name, visiting)
                for fi in _iter_fields(rec.node):
                    fields_by_name[fi.name] = _keep_model_declared(
                        fi._replace(node=None), fields_by_name.get(fi.name)
                    )
        else:
            # The un-scannable case: an SDK contract, whose source is not part of
            # a consumer repo's AST. Its own fields are mirrored statically, and
            # so is the one annotation fact a caller cannot recover from a bare
            # name — which of them the SDK marks.
            sdk_fields = SDK_CONTRACT_BASE_FIELDS.get(
                name, SDK_TEMPLATE_CONTRACT_FIELDS.get(name, ())
            )
            for sdk_field in sdk_fields:
                fields_by_name[sdk_field.name] = _keep_model_declared(
                    _FieldInfo(
                        name=sdk_field.name,
                        canonical_type=sdk_field.canonical_type,
                        status=sdk_field.status,
                        node=None,
                        model_declared=(
                            sdk_field.name in SDK_MODEL_BACKED_ARTIFACT_FIELDS
                        ),
                    ),
                    fields_by_name.get(sdk_field.name),
                )
        merged.add(name)

        visiting.discard(name)

    visiting: set[str] = {classdef.name}
    for base in reversed(classdef.bases):
        bname = _base_name(base)
        if bname is not None:
            merge_ancestor(aliases.get(bname, bname), visiting)

    # Fields declared directly on classdef always win over inherited ones —
    # except for the marker, which is sticky (see `_keep_model_declared`).
    for fi in _iter_fields(classdef):
        fields_by_name[fi.name] = _keep_model_declared(fi, fields_by_name.get(fi.name))

    return list(fields_by_name.values())


# ── Entrypoint contract discovery ─────────────────────────────────────────────


def collect_entrypoint_contract_names(
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
