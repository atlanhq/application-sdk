"""K006 ManifestContractFieldMismatch — check implementation.

Cross-references every ``$.extract.outputs.<field>`` reference in the
committed ``app/generated/**/manifest.json`` DAG against the entrypoint's
Python ``Output`` contract (resolved across its full inheritance chain via the
shared :func:`resolve_contract_fields`, so a field supplied by an SDK mixin
such as ``PublishInputMixin`` counts as declared). A field the manifest
requires but the contract does not declare — anywhere in its MRO — is the
exact class of bug this rule exists to catch (BLDX-1527): the field is
load-bearing for a platform-orchestrated pipeline step (e.g. ``publish``) but
nothing in either language's own tooling notices its removal.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
)
from conformance.suite.checks._entrypoint_contract_fields import resolve_contract_fields
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

from ._code_outputs import (
    CodeOutputScan,
    EntrypointOutput,
    scan_file_for_entrypoint_outputs,
)
from ._manifest_refs import ManifestDag, read_manifest

_RULE_ID = "K006"


def _manifest_paths_for_contract(root: Path, contract) -> list[Path]:  # noqa: ANN001
    """Return the ``manifest.json`` paths implied by the P016 contract-scan mode."""
    generated = root / "app" / "generated"
    if contract.mode == "single":
        return [generated / "manifest.json"]
    if contract.mode == "multi":
        return [generated / name / "manifest.json" for name in sorted(contract.names)]
    return []


def _target_entrypoint(
    manifest: ManifestDag,
    mode: str,
    entrypoints: list[EntrypointOutput],
) -> EntrypointOutput | None:
    """Resolve which entrypoint a manifest's own (``"extract"``) node belongs to.

    ``single`` mode: unambiguous only when code declares exactly one
    entrypoint (mirrors P016 — a single-EP contract's entrypoint name is
    otherwise unconstrained).

    ``multi`` mode: the manifest's parent directory name is the contract's
    entry-point name (the P016 invariant); match it against the wire name.
    """
    if mode == "single":
        if len(entrypoints) != 1:
            return None
        return entrypoints[0]

    ep_name = Path(manifest.manifest_path).parent.name
    matches = [ep for ep in entrypoints if ep.wire_name == ep_name]
    if len(matches) != 1:
        return None
    return matches[0]


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Cross-reference manifest JSONPath refs against Python Output contracts.

    No-ops when ``app/generated/`` is absent, when no manifest carries a
    parseable ``dag``, or when no ``@entrypoint``/implicit ``App.run()`` is
    found in code — all conservative (WARN rule; prefers a false negative to a
    false positive on a repo shape this check does not understand).
    """
    if not (root / "app" / "generated").is_dir():
        return []

    contract = scan_contract_entrypoints(root)
    if contract.mode == "absent":
        return []

    manifest_paths = _manifest_paths_for_contract(root, contract)
    manifests = [
        m for m in (read_manifest(p, root) for p in manifest_paths) if m is not None
    ]
    if not manifests:
        return []

    # Pass 1: parse every file; build the cross-file class registry (`by_name`)
    # and per-file directives/aliases, all keyed by repo-relative path so a
    # class's *own* file's aliases can be looked up later regardless of which
    # file is currently being iterated (mirrors scan_contract_compat, but
    # keyed by the relative string directly since ClassRecord.file already is).
    file_trees: dict[str, ast.AST] = {}
    file_directives: dict[str, dict[int, _IgnoreDirective]] = {}
    file_aliases: dict[str, dict[str, str]] = {}
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

    # Pass 2: collect the per-entrypoint (wire_name -> Output class) map.
    code = CodeOutputScan()
    app_cache: dict[str, bool | None] = {}
    for rel, tree in file_trees.items():
        if not isinstance(tree, ast.Module):
            continue
        prov = collect_import_provenance(tree)
        scan_file_for_entrypoint_outputs(
            tree, rel, file_aliases.get(rel, {}), prov, by_name, app_cache, code
        )

    if not code.entrypoints:
        return []

    # Pass 3: for each manifest, resolve its target entrypoint's Output field
    # set and diff against the manifest's own-node references.
    findings: list[Finding] = []

    for manifest in manifests:
        target = _target_entrypoint(manifest, contract.mode, code.entrypoints)
        if target is None or target.output_class_name is None:
            continue

        output_rec = by_name.get(target.output_class_name)
        if output_rec is None:
            continue  # Output class not resolvable in scanned source — skip.

        output_aliases = file_aliases.get(output_rec.file, {})
        live_fields = resolve_contract_fields(output_rec.node, output_aliases, by_name)
        live_names = {f.name for f in live_fields}
        directives = file_directives.get(output_rec.file, {})

        missing_fields = sorted(
            {ref.field for ref in manifest.own_refs() if ref.field not in live_names}
        )
        for missing_field in missing_fields:
            findings.append(
                make_finding(
                    filename=output_rec.file,
                    rule_id=_RULE_ID,
                    node=output_rec.node,
                    message=(
                        f"'{manifest.manifest_path}' references "
                        f"'$.extract.outputs.{missing_field}', but "
                        f"'{output_rec.name}' (the entrypoint's Output contract) "
                        f"does not declare a '{missing_field}' field — directly or "
                        "via an inherited base/mixin. The Automation Engine resolves "
                        "this JSONPath at runtime against the entrypoint's returned "
                        "Output; a missing field fails the dependent pipeline step "
                        "(e.g. publish) with an unresolved-JSONPath error. Declare "
                        f"'{missing_field}' on '{output_rec.name}', or mix in the SDK "
                        "contract base that supplies it (e.g. "
                        "'application_sdk.contracts.base.PublishInputMixin' for the "
                        "publish-state fields). Never hand-edit the generated "
                        "manifest.json to work around this — it is a pkl eval output. "
                        "Suppress with '# conformance: ignore[K006] <reason>' on the "
                        "Output class definition."
                    ),
                    directives=directives,
                )
            )

    return findings
