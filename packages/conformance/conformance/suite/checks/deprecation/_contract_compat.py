"""B005 NonAdditiveContractChange / B006 StaleContractLedger — AST-based checker.

Entrypoint-only scope: only Input/Output contracts referenced by ``@entrypoint``
methods or implicit ``App.run()`` are gated.  ``@task`` contracts are excluded.

Entrypoint-contract discovery and field extraction (including the full
inheritance-hierarchy walk) live in the neutral ``suite.checks._contract_fields``
module — shared with the ledger generator, and expected to also back the
K-series contract-toolkit checks. This module owns only the B005/B006
finding-emission logic: comparing resolved live fields against the committed
ledger.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
)
from conformance.suite.checks._contract_fields import (
    collect_entrypoint_contract_names,
    resolve_contract_fields,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
)
from conformance.suite.schema.findings import Finding

from ._ledger_schema import ContractField, ContractLedger

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

    entrypoint_names = collect_entrypoint_contract_names(file_trees, by_name)

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
            live_fields = resolve_contract_fields(class_node, aliases, by_name)
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
