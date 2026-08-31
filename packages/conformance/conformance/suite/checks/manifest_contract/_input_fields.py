"""K018 ManifestArgNotDeclaredOnInputContract — check implementation.

The Input-side mirror of K006. Where K006 verifies that a manifest's
``$.extract.outputs.<field>`` references resolve against the entrypoint's
``Output`` contract, K018 verifies the other direction: that every argument the
Automation Engine will *send* to the ``extract`` node can actually be received
by the entrypoint's ``Input`` contract.

A key the contract cannot receive is dropped by Pydantic before ``model_dump()``
and the entrypoint runs on the field's default. For a filter that default is
empty, and an empty include-filter means *crawl everything* — the failure is
fail-open, so it surfaces as a catalogue flood rather than an error
(CONNECT-1318).

Three shapes all satisfy the contract, and the rule must accept every one —
flagging any of them is how a WARN rule earns a reputation for noise and never
graduates:

1. the key is declared on the contract's resolved base chain;
2. the contract (or an ancestor) sets real Pydantic ``extra="allow"``, which
   preserves undeclared keys in ``model_extra`` for the app to read; or
3. the contract (or an ancestor) defines a ``@model_validator(mode="before")``
   — the prescribed remedy for the flattening break is exactly such a validator
   folding flat keys into ``metadata`` *without* declaring each flat field.

``allow_unbounded_fields=True`` is deliberately **not** accepted as satisfying,
and the distinction from (2) is the whole point: that SDK class kwarg only
suppresses the unknown-key *error*, it does not set Pydantic ``extra="allow"``,
so the keys are still dropped before ``model_dump()``. Conflating the two is the
original misreading this rule exists to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
)
from conformance.suite.checks._entrypoint_contract_classes import (
    CodeContractScan,
    EntrypointContract,
    scan_file_for_entrypoint_contracts,
)
from conformance.suite.checks._entrypoint_contract_fields import resolve_contract_fields
from conformance.suite.checks._sdk_contract_mixins import (
    SDK_CONTRACT_BASE_FIELDS,
    SDK_TEMPLATE_CONTRACT_FIELDS,
)
from conformance.suite.checks.entrypoint_alignment._contract_entrypoints import (
    scan_contract as scan_contract_entrypoints,
)
from conformance.suite.checks.prescriptions._decorator_provenance import (
    collect_import_provenance,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)
from conformance.suite.schema.findings import Finding

from ._manifest_args import ManifestArgs, collect_arg_keys
from ._manifest_refs import manifest_paths_for_contract

_RULE_ID = "K018"

# Validator modes that see the raw inbound payload before field coercion, and
# can therefore fold an undeclared key into a declared one.
_RAW_PAYLOAD_MODES = frozenset({"before", "wrap"})

_MODEL_VALIDATOR = "model_validator"

# The SDK base every SQL connector's extraction contract derives from. Used as
# the fallback anchor when no @entrypoint is declared in the app's own source.
_EXTRACTION_INPUT_BASE = "ExtractionInput"

# Args the Automation Engine injects into every extract node and that no
# contract in the fleet declares — measured across 34 connector apps on
# 2026-08-31. The credential value is resolved by the SDK credentials-ingress
# path, not by the entrypoint contract, which reads credential_guid /
# credential_ref instead. Reporting these would put an unactionable finding on
# every app and bury the real ones.
_PLATFORM_INJECTED_ARGS = frozenset({"credential"})


def _target_entrypoint(
    manifest_path: str,
    mode: str,
    entrypoints: list[EntrypointContract],
) -> EntrypointContract | None:
    """Resolve which entrypoint a manifest's ``extract`` node belongs to.

    Same resolution K006 uses — ``single`` mode is unambiguous only with exactly
    one declared entrypoint; ``multi`` mode matches the manifest's parent
    directory name against the entrypoint's wire name (the P016 invariant).
    """
    if mode == "single":
        return entrypoints[0] if len(entrypoints) == 1 else None

    ep_name = Path(manifest_path).parent.name
    matches = [ep for ep in entrypoints if ep.wire_name == ep_name]
    return matches[0] if len(matches) == 1 else None


def _is_raw_payload_validator(deco: ast.expr) -> bool:
    """True for ``@model_validator(mode="before"|"wrap")`` in any aliased form."""
    if not isinstance(deco, ast.Call):
        return False
    func = deco.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else None
    )
    if name != _MODEL_VALIDATOR:
        return False
    for kw in deco.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            if kw.value.value in _RAW_PAYLOAD_MODES:
                return True
    return False


def _is_extra_allow_keyword(kw: ast.keyword) -> bool:
    return (
        kw.arg == "extra"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value == "allow"
    )


def _is_extra_allow_mapping(node: ast.expr) -> bool:
    """True for ``{"extra": "allow"}``."""
    if not isinstance(node, ast.Dict):
        return False
    return any(
        isinstance(k, ast.Constant)
        and k.value == "extra"
        and isinstance(v, ast.Constant)
        and v.value == "allow"
        for k, v in zip(node.keys, node.values)
        if k is not None
    )


def _class_allows_extra(classdef: ast.ClassDef) -> bool:
    """True when the class sets real Pydantic ``extra="allow"``.

    Unlike the SDK's ``allow_unbounded_fields=True`` class kwarg — which only
    silences the unknown-key error — this genuinely preserves undeclared keys
    in ``model_extra``, so the app can still read them and the rule must stay
    silent. Recognised in all three forms Pydantic accepts:

    * ``model_config = ConfigDict(extra="allow")``
    * ``model_config = {"extra": "allow"}``
    * ``class Foo(Base, extra="allow")``
    """
    if any(_is_extra_allow_keyword(kw) for kw in classdef.keywords):
        return True

    for item in classdef.body:
        targets: list[ast.expr] = []
        if isinstance(item, ast.Assign):
            targets = list(item.targets)
        elif isinstance(item, ast.AnnAssign):
            targets = [item.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "model_config" for t in targets):
            continue
        value = item.value
        if value is None:
            continue
        if isinstance(value, ast.Call) and any(
            _is_extra_allow_keyword(kw) for kw in value.keywords
        ):
            return True
        if _is_extra_allow_mapping(value):
            return True
    return False


def _class_has_raw_payload_validator(classdef: ast.ClassDef) -> bool:
    return any(
        _is_raw_payload_validator(deco)
        for item in classdef.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        for deco in item.decorator_list
    )


def _is_known_sdk_contract(name: str) -> bool:
    """True when *name* is an SDK contract the static registries can resolve.

    Two registries, both consulted by ``resolve_contract_fields``:
    ``SDK_CONTRACT_BASE_FIELDS`` for the bases/mixins in
    ``application_sdk.contracts.base``, and ``SDK_TEMPLATE_CONTRACT_FIELDS`` for
    the template contracts (``ExtractionInput`` and friends), whose entries are
    pre-flattened across their own base chains.
    """
    return name in SDK_CONTRACT_BASE_FIELDS or name in SDK_TEMPLATE_CONTRACT_FIELDS


def _walk_chain(
    rec: ClassRecord,
    by_name: dict[str, ClassRecord],
) -> tuple[list[ast.ClassDef], bool]:
    """Return (in-repo ClassDefs across the chain, whether it fully resolved).

    An ancestor is *resolved* when it is either defined in the scanned repo or
    recorded in :data:`SDK_CONTRACT_BASE_FIELDS`. Anything else — an SDK base
    the registry does not mirror, a third-party model — makes the chain
    incomplete, and the caller must then skip rather than guess. That matches
    K006's stated preference for a false negative over a false positive on a
    repo shape the check does not understand.
    """
    nodes: list[ast.ClassDef] = []
    fully_resolved = True
    visiting: set[str] = set()

    def walk(name: str) -> None:
        nonlocal fully_resolved
        if name in visiting:
            return  # cycle — mirrors resolve_contract_fields' guard
        visiting.add(name)

        ancestor = by_name.get(name)
        if ancestor is not None:
            nodes.append(ancestor.node)
            for base_name in ancestor.bases:
                walk(base_name)
        elif not _is_known_sdk_contract(name):
            fully_resolved = False

    nodes.append(rec.node)
    for base in rec.bases:
        walk(base)
    return nodes, fully_resolved


def _chain_reaches(
    rec: ClassRecord,
    by_name: dict[str, ClassRecord],
    target: str,
) -> bool:
    """True when *rec*'s ancestor chain reaches a base named *target*."""
    seen: set[str] = set()

    def walk(name: str) -> bool:
        if name == target:
            return True
        if name in seen:
            return False
        seen.add(name)
        ancestor = by_name.get(name)
        return any(walk(b) for b in ancestor.bases) if ancestor is not None else False

    return any(walk(base) for base in rec.bases)


def _reference_count(name: str, trees: dict[str, ast.AST]) -> int:
    """How many times *name* is mentioned outside its own ``class`` statement.

    A ``ClassDef`` contributes no mention of itself, so a contract that is never
    annotated, imported, subclassed or instantiated scores zero — it is dead
    code and cannot be what an entrypoint binds.
    """
    count = 0
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == name:
                count += 1
            elif isinstance(node, ast.Attribute) and node.attr == name:
                count += 1
            elif isinstance(node, ast.ImportFrom) and any(
                a.name == name for a in node.names
            ):
                count += 1
    return count


def _sole_extraction_input(
    by_name: dict[str, ClassRecord],
    trees: dict[str, ast.AST],
) -> ClassRecord | None:
    """The app's one live ``ExtractionInput`` descendant, or ``None`` if ambiguous.

    Most SQL connectors never write ``@entrypoint`` in their own source: the app
    class extends an SDK template (``BaseMetadataExtractor``) and inherits the
    entrypoint from it, so the decorator-based resolution finds nothing. That is
    the shape of the app in the motivating incident, so without this fallback
    K018 would be silent on exactly the family it exists to protect.

    Anchoring on ``ExtractionInput`` rather than ``Input`` keeps the answer
    narrow — an app's ``TaskInput`` / ``CatalogTaskInput`` helpers descend from
    ``Input`` and are correctly excluded.

    Apps commonly carry *two* descendants: a hand-written contract and the
    toolkit-generated ``AppInputContract``. Only one is wired to the entrypoint,
    and which one varies across the fleet — some apps import the generated
    class, others leave it unreferenced beside a hand-written binding. An
    unreferenced class cannot be the live one, so candidates with no mention
    anywhere are dropped before the uniqueness test. Still ambiguous after that
    (two live descendants, or none) means skip rather than guess.
    """
    candidates = [
        rec
        for rec in by_name.values()
        if _chain_reaches(rec, by_name, _EXTRACTION_INPUT_BASE)
    ]
    if len(candidates) > 1:
        candidates = [
            rec for rec in candidates if _reference_count(rec.name, trees) > 0
        ]
    return candidates[0] if len(candidates) == 1 else None


def _resolved_field_names(
    rec: ClassRecord,
    file_aliases: dict[str, dict[str, str]],
    by_name: dict[str, ClassRecord],
) -> set[str]:
    aliases = file_aliases.get(rec.file, {})
    return {f.name for f in resolve_contract_fields(rec.node, aliases, by_name)}


def _pair_manifests_with_contracts(
    manifests: list[ManifestArgs],
    mode: str,
    code: CodeContractScan,
    by_name: dict[str, ClassRecord],
    trees: dict[str, ast.AST],
) -> list[tuple[ManifestArgs, ClassRecord]]:
    """Map each manifest to the Input contract its ``extract`` node binds.

    Two resolution paths, in order of confidence:

    1. an explicit ``@entrypoint`` in the app's own source names the contract
       (the same resolution K006 uses); or
    2. the app inherits its entrypoint from an SDK template, so there is no
       decorator to read — fall back to the app's sole ``ExtractionInput``
       descendant. Restricted to single-entrypoint apps: in multi-entrypoint
       mode there is no way to map one contract onto N manifests.
    """
    if code.entrypoints:
        pairs: list[tuple[ManifestArgs, ClassRecord]] = []
        for manifest in manifests:
            target = _target_entrypoint(manifest.manifest_path, mode, code.entrypoints)
            if target is None or target.input_class_name is None:
                continue
            rec = by_name.get(target.input_class_name)
            if rec is not None:
                pairs.append((manifest, rec))
        return pairs

    if mode != "single" or len(manifests) != 1:
        return []
    rec = _sole_extraction_input(by_name, trees)
    return [(manifests[0], rec)] if rec is not None else []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Diff each manifest's ``extract`` arg keys against its Input contract.

    No-ops when ``app/generated/`` is absent, no manifest carries a parseable
    ``dag.extract.inputs.args``, no Input contract can be bound to a manifest,
    or that contract's inheritance chain cannot be fully resolved.
    """
    if not (root / "app" / "generated").is_dir():
        return []

    contract = scan_contract_entrypoints(root)
    if contract.mode == "absent":
        return []

    manifests = [
        m
        for m in (
            collect_arg_keys(p, root)
            for p in manifest_paths_for_contract(root, contract)
        )
        if m is not None
    ]
    if not manifests:
        return []

    file_trees: dict[str, ast.AST] = {}
    file_directives: dict[str, dict[int, _IgnoreDirective]] = {}
    file_aliases: dict[str, dict[str, str]] = {}
    by_name: dict[str, ClassRecord] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)

        file_trees[rel] = tree
        file_directives[rel] = _parse_directives(text)
        aliases = collect_import_aliases(tree) if isinstance(tree, ast.Module) else {}
        file_aliases[rel] = aliases
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)

    code = CodeContractScan()
    app_cache: dict[str, bool | None] = {}
    for rel, tree in file_trees.items():
        if not isinstance(tree, ast.Module):
            continue
        prov = collect_import_provenance(tree)
        scan_file_for_entrypoint_contracts(
            tree, rel, file_aliases.get(rel, {}), prov, by_name, app_cache, code
        )

    pairs = _pair_manifests_with_contracts(
        manifests, contract.mode, code, by_name, file_trees
    )

    findings: list[Finding] = []

    for manifest, input_rec in pairs:
        chain_nodes, fully_resolved = _walk_chain(input_rec, by_name)
        if not fully_resolved:
            continue  # incomplete picture — stay silent rather than guess.
        if any(_class_allows_extra(node) for node in chain_nodes):
            continue  # real Pydantic extra="allow" keeps undeclared keys.
        if any(_class_has_raw_payload_validator(node) for node in chain_nodes):
            continue  # a before/wrap validator can fold any undeclared key.

        declared = _resolved_field_names(input_rec, file_aliases, by_name)
        directives = file_directives.get(input_rec.file, {})

        findings.extend(
            _make_finding(manifest, arg_key, input_rec, directives)
            for arg_key in sorted(manifest.keys() - declared - _PLATFORM_INJECTED_ARGS)
        )

    return findings


def _make_finding(
    manifest: ManifestArgs,
    arg_key: str,
    input_rec: ClassRecord,
    directives: dict[int, _IgnoreDirective],
) -> Finding:
    depth = next(
        ("args.metadata" if a.nested else "args")
        for a in manifest.args
        if a.key == arg_key
    )
    return make_finding(
        filename=input_rec.file,
        rule_id=_RULE_ID,
        node=input_rec.node,
        message=(
            f"'{manifest.manifest_path}' sends '{depth}.{arg_key}' to the extract "
            f"node, but '{input_rec.name}' (the entrypoint's Input contract) does "
            f"not declare a '{arg_key}' field — directly or via an inherited "
            "base/mixin — and defines no @model_validator(mode='before'). Pydantic "
            "drops the key before model_dump(), so the entrypoint runs on the "
            f"field's default. For a filter that default is empty, and an empty "
            "include-filter means crawl everything (CONNECT-1318). Declare "
            f"'{arg_key}' on '{input_rec.name}', mix in the SDK base that supplies "
            "it (e.g. 'ExtractionInput' for the filter fields), or add a "
            "@model_validator(mode='before') folding flat keys into 'metadata' — "
            "let nested win over flat so pre-0.9.0 workflow specs still validate. "
            "Note 'allow_unbounded_fields=True' does NOT satisfy this: it only "
            "suppresses the unknown-key error, it does not set extra='allow'. "
            "Never hand-edit the generated manifest.json to work around this — it "
            "is a pkl eval output. Suppress with "
            "'# conformance: ignore[K018] <reason>' on the Input class definition."
        ),
        directives=directives,
    )
