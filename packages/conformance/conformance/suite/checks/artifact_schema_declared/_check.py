"""K016 EntrypointArtifactSchemaMissing — check implementation.

Cross-references every ``FileReference`` field on an entrypoint's boundary
contracts (``input``/``return``, resolved across their full inheritance chain
via the shared :func:`resolve_contract_fields`) against the ``artifactSchemas``
declarations the toolkit rendered to ``app/generated/**/artifact_schemas.json``.
A boundary artifact with no declaration is the defect this rule exists to catch
(ADR-0020): the producer's idea of the file's shape and the consumer's are
independent beliefs, and nothing in either language's own tooling notices when
they diverge.

Structure mirrors K006 (``manifest_contract``) closely, because the shape of the
problem is the same — a committed generated artifact cross-referenced against a
Python contract resolved through its MRO — and sharing that shape is what lets
both rules reuse the same class-registry and contract-field primitives.
"""

from __future__ import annotations

import ast
import re
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

from ._declarations import read_declarations

_RULE_ID = "K016"

#: Matches ``FileReference`` as a whole identifier anywhere in a canonicalised
#: annotation string, so ``FileReference``, ``FileReference | None``,
#: ``list[FileReference]`` and ``dict[str, FileReference]`` all count: a field
#: that can carry an artifact is a field whose artifact needs describing.
#:
#: Matching the canonical *string* rather than the AST means an import alias
#: (``from ... import FileReference as FileRef``) is not recognised. That is a
#: false negative on an unidiomatic import, which is the direction a WARN rule
#: errs in — never a false positive on a field that is not an artifact.
_FILE_REFERENCE_RE = re.compile(r"\bFileReference\b")


def _boundary_contract_names(ep: EntrypointContract) -> list[tuple[str, str]]:
    """Return this entrypoint's ``(direction, class name)`` boundary contracts.

    Both directions are public.  For a cross-app hand-off the **consumer**
    declares what it requires of its input, so an entrypoint's Input carries
    artifact declarations exactly like its Output does.
    """
    pairs: list[tuple[str, str]] = []
    if ep.input_class_name:
        pairs.append(("input", ep.input_class_name))
    if ep.output_class_name:
        pairs.append(("output", ep.output_class_name))
    return pairs


def _target_entrypoints(
    mode: str,
    contract_names: frozenset[str],
    entrypoints: list[EntrypointContract],
) -> list[EntrypointContract]:
    """Return the entrypoints whose declarations file this rule can locate.

    ``single`` mode: **every** entrypoint, including a route/card-split app's
    (one marketplace card, several ``@entrypoint``\\ s the DAG invokes by
    ``workflow_type``).  There is exactly one flat declarations file and it is
    the whole app's declaration set, so there is nothing to disambiguate: each
    entrypoint's boundary is checked against it, and a missing key is fixed by
    adding it to the app's one ``artifactSchemas`` block.

    This is where K016 deliberately parts company with K006's
    ``_target_entrypoint``, which no-ops on the same shape.  K006 must decide
    *which entrypoint's* ``Output`` a given manifest node refers to — genuinely
    underdetermined with one flat manifest and several entrypoints.  K016 asks a
    different question, "is this field declared anywhere the app declares
    things", and in single mode there is only one such place.  No-oping here
    would also make the rule disagree with the SDK's registration-time guard,
    which checks card-split entrypoints against that same flat file — a gap
    where an app is warned at worker build about something review never raised.

    ``multi`` mode: an entrypoint is checkable when its wire name is one of the
    contract's own entrypoint names — i.e. a bundle subdirectory exists for it.
    An entrypoint in code with no matching subdir is P016's finding to report,
    not this rule's.
    """
    if mode == "single":
        return entrypoints
    return [ep for ep in entrypoints if ep.wire_name in contract_names]


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Report boundary ``FileReference`` fields with no artifact-schema declaration.

    No-ops when ``app/generated/`` is absent, when the contract scan cannot
    classify the tree, when no ``@entrypoint``/implicit ``App.run()`` is found in
    code, or when an entrypoint's declarations file cannot be located — all
    conservative, matching K006's policy of preferring a false negative to a
    false positive on a repo shape the check does not understand.
    """
    if not (root / "app" / "generated").is_dir():
        return []

    contract = scan_contract_entrypoints(root)
    if contract.mode == "absent":
        return []

    # Pass 1: parse every file; build the cross-file class registry (`by_name`)
    # and per-file directives/aliases, keyed by repo-relative path so a class's
    # *own* file's aliases can be looked up regardless of which file is being
    # iterated. Mirrors K006's pass 1 exactly.
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

    # Pass 2: collect the per-entrypoint boundary-contract map.
    code = CodeContractScan()
    app_cache: dict[str, bool | None] = {}
    for rel, tree in file_trees.items():
        if not isinstance(tree, ast.Module):
            continue
        prov = collect_import_provenance(tree)
        scan_file_for_entrypoint_contracts(
            tree, rel, file_aliases.get(rel, {}), prov, by_name, app_cache, code
        )

    if not code.entrypoints:
        return []

    # Pass 3: for each locatable entrypoint, diff its boundary artifacts against
    # what that entrypoint's declarations file declares.
    findings: list[Finding] = []

    for ep in _target_entrypoints(contract.mode, contract.names, code.entrypoints):
        declarations = read_declarations(root, contract.mode, ep.wire_name)
        if not declarations.checkable:
            # A present-but-malformed declarations file means "we cannot tell",
            # not "nothing is declared" — reporting it as the latter would turn
            # one bad JSON blob into a finding on every boundary field. The
            # malformed artifact itself is C002/K005 territory.
            continue
        declared = declarations.keys
        schemas_path = declarations.path
        if schemas_path is None:  # pragma: no cover — set for every checkable status
            continue
        try:
            rel_schemas_path = str(schemas_path.relative_to(root))
        except ValueError:  # pragma: no cover — schemas_path is built from root
            rel_schemas_path = str(schemas_path)
        ep_label = ep.wire_name or "run"

        for direction, class_name in _boundary_contract_names(ep):
            rec = by_name.get(class_name)
            if rec is None:
                continue  # Contract class not resolvable in scanned source — skip.

            aliases = file_aliases.get(rec.file, {})
            directives = file_directives.get(rec.file, {})
            fields = resolve_contract_fields(rec.node, aliases, by_name)

            for field in fields:
                if not _FILE_REFERENCE_RE.search(field.canonical_type):
                    continue
                if field.name in declared:
                    continue
                findings.append(
                    make_finding(
                        filename=rec.file,
                        rule_id=_RULE_ID,
                        # Inherited fields carry node=None (the ancestor's AST
                        # belongs to another file), so anchor those on the
                        # contract class itself.
                        node=field.node if field.node is not None else rec.node,
                        message=(
                            f"'{rec.name}' is the '{ep_label}' entry point's "
                            f"{direction} contract and declares a FileReference "
                            f"field '{field.name}', but "
                            f"'{rel_schemas_path}' declares no artifact schema "
                            f"for it. An entry point's contracts are a public "
                            f"boundary — another app or the DAG reads this "
                            f"artifact, and nothing checks that what was written "
                            f"matches what the reader expects. Declare it in the "
                            f"pkl contract and regenerate:\n"
                            f"\n"
                            f"    artifactSchemas {{\n"
                            f'      ["{field.name}"] = new ArtifactSchema {{\n'
                            f'        format = "ndjson"  // or "parquet"\n'
                            f"        fields {{ new ArtifactField {{ ... }} }}\n"
                            f"      }}\n"
                            f"    }}\n"
                            f"\n"
                            f"Never hand-edit the generated "
                            f"'{rel_schemas_path}' — it is a pkl eval output and "
                            f"the next toolkit run reverts the edit. Internal "
                            f"'@task' contracts are exempt from this rule; "
                            f"entry-point contracts are not. Suppress with "
                            f"'# conformance: ignore[K016] <reason>' on the "
                            f"field (or on the contract class, for a field "
                            f"inherited from a base)."
                        ),
                        directives=directives,
                    )
                )

    return findings
