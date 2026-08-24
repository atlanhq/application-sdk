"""K015 LegacyWorkflowTypeContractDrift — check implementation (CONNECT-1081).

An inbound-only Temporal workflow type alias is declared in two places once an
app carries a contract tree: the generated
``app/generated/**/manifest.json`` ``legacy_workflow_types`` block, which is the
contracted declaration site, and the SDK's ``App.legacy_workflow_types`` class
attribute, which is what the worker actually registers.

Only the class attribute changes runtime behaviour. Only the manifest block is
read by P016 when it decides whether a bare DAG node routes an entry point. So a
disagreement between them is silent in both directions: an alias present only in
code is one P016 no longer credits, and an alias present only in the manifest is
one the worker rejects at dispatch while the contract advertises it. This check
holds the two in agreement.

Apps with no ``app/generated/`` tree are out of scope — for them the class
attribute is the only declaration site and there is nothing to compare.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.entrypoint_alignment._code_entrypoints import (
    AppClassLocation,
    scan_paths_for_entrypoints,
)
from conformance.suite.checks.entrypoint_alignment._contract_entrypoints import (
    LegacyAliasDeclaration,
    load_manifest_document,
    parse_legacy_aliases,
)
from conformance.suite.checks.entrypoint_alignment._contract_entrypoints import (
    scan_contract as scan_contract_entrypoints,
)
from conformance.suite.schema.findings import Finding

from ._manifest_refs import manifest_paths_for_contract

_RULE_ID = "K015"


def _format_pairs(pairs: frozenset[tuple[str, str]]) -> str:
    return ", ".join(f"{alias} -> {target}" for alias, target in sorted(pairs))


def _finding(
    anchor: AppClassLocation,
    message: str,
    directives_by_file: dict[str, dict[int, _IgnoreDirective]],
) -> Finding:
    return make_finding(
        filename=anchor.filename,
        rule_id=_RULE_ID,
        node=anchor.node,
        message=message,
        directives=directives_by_file.get(anchor.filename, {}),
    )


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Compare the manifest ``legacy_workflow_types`` block against the SDK declaration.

    No-ops when ``app/generated/`` is absent or unparseable, when no ``App``
    subclass is found in code, and when none of the ``App`` subclasses found is the
    one this manifest belongs to — there is then no pair of declarations to hold in
    agreement, and reporting drift against a repo shape this check does not
    understand would be a false positive.
    """
    if not (root / "app" / "generated").is_dir():
        return []

    contract = scan_contract_entrypoints(root)
    if contract.mode == "absent":
        return []

    documents = [
        (path, document)
        for path, document in (
            (path, load_manifest_document(path))
            for path in manifest_paths_for_contract(root, contract)
        )
        if document is not None
    ]
    if not documents:
        return []

    code, directives_by_file = scan_paths_for_entrypoints(paths, root)
    if not code.app_classes:
        return []

    # Scope to the App this manifest belongs to. A repo may define several App
    # subclasses; pooling their declarations against one manifest both invents drift
    # (a sibling App's aliases read as missing from a manifest that was never meant
    # to carry them) and hides it (a sibling can satisfy a manifest alias the owning
    # App never registered). Identity comes from the manifest, the same source P016
    # uses. When the manifest yields no identity there is nothing to scope by, so
    # fall back to every class rather than silently checking nothing.
    owning = [
        app_class
        for app_class in code.app_classes
        if app_class.app_name in contract.own_app_names
    ]
    if contract.own_app_names and not owning:
        return []
    app_classes = owning or code.app_classes

    findings: list[Finding] = []

    # A declaration the scan cannot read statically blocks the comparison
    # outright — report that rather than a mismatch the check cannot prove.
    owned = {id(app_class) for app_class in app_classes}
    unresolved = [c for c in code.unresolved_aliases if id(c) in owned]
    for unreadable in unresolved:
        findings.append(
            _finding(
                unreadable,
                "legacy_workflow_types (or legacy_workflow_types_removal_version) is "
                "not a literal declaration, so it cannot be compared against the "
                "manifest's legacy_workflow_types block. Declare it inline, e.g. "
                'legacy_workflow_types = {"LegacyType": "entry-point-name"}. '
                "Suppress with '# conformance: ignore[K015] <reason>' if unavoidable.",
                directives_by_file,
            )
        )
    if unresolved:
        return findings

    declared: list[LegacyAliasDeclaration] = [
        parse_legacy_aliases(document) for _, document in documents
    ]

    anchor = app_classes[0]

    # Every generated manifest carries the same app-level block, so a divergence
    # between copies means one entry point was regenerated and another was not.
    distinct = {(d.aliases, d.removal_version) for d in declared}
    if len(distinct) > 1:
        findings.append(
            _finding(
                anchor,
                "the generated manifests disagree on legacy_workflow_types: "
                + "; ".join(
                    f"{path.relative_to(root)} declares "
                    f"[{_format_pairs(d.aliases) or 'none'}]"
                    for (path, _), d in zip(documents, declared)
                )
                + ". The block is app-level, so every entry point's manifest must "
                "carry the same copy — re-run the contract generation.",
                directives_by_file,
            )
        )
        return findings

    manifest_declaration = declared[0]
    code_aliases = frozenset(
        (alias, target)
        for app_class in app_classes
        for alias, target in app_class.legacy_aliases.items()
    )

    missing_from_manifest = code_aliases - manifest_declaration.aliases
    if missing_from_manifest:
        findings.append(
            _finding(
                anchor,
                "legacy_workflow_types declares aliases the app contract does not: "
                f"[{_format_pairs(missing_from_manifest)}]. The generated manifest is "
                "the contracted declaration site, and P016 routes off it — an alias "
                "missing there reads as unrouted. Declare it in the contract's "
                "legacyWorkflowTypes and re-run the contract generation.",
                directives_by_file,
            )
        )

    missing_from_code = manifest_declaration.aliases - code_aliases
    if missing_from_code:
        findings.append(
            _finding(
                anchor,
                "the app contract declares legacy_workflow_types the SDK App does not: "
                f"[{_format_pairs(missing_from_code)}]. Only the class attribute "
                "registers the alias with the worker, so a caller dispatching one of "
                "these is rejected while the contract advertises it. Add it to "
                "legacy_workflow_types, or drop it from the contract.",
                directives_by_file,
            )
        )

    # Every alias-bearing class must name the manifest's version, not just one of
    # them: with two Apps declaring "4.2.0" and no expiry, a membership test would
    # accept the manifest against either and let the other diverge unreported.
    disagreeing = {
        app_class.legacy_removal_version
        for app_class in app_classes
        if app_class.legacy_aliases
        and app_class.legacy_removal_version != manifest_declaration.removal_version
    }
    if disagreeing:
        findings.append(
            _finding(
                anchor,
                "legacy_workflow_types_removal_version disagrees with the contract: "
                f"code declares {sorted(v or 'no expiry' for v in disagreeing)!r}, "
                "the manifest declares "
                f"{manifest_declaration.removal_version or 'no expiry'!r}. The expiry "
                "is what turns a drained alias into a loud decision rather than drift, "
                "so the two sites must name the same version.",
                directives_by_file,
            )
        )

    return findings
