"""P016 check implementation — apply the entry-point alignment invariant.

Applies the per-repo invariant and builds :class:`~conformance.suite.schema.findings.Finding`
objects via :func:`~conformance.suite.checks._ast_common.make_finding` so
inline ``# conformance: ignore[P016]`` suppression works identically to every
other AST-based rule.

Invariant
---------
Let ``code`` = all ``@entrypoint`` wire names collected from Python source.
Let ``contract`` = subdir names under ``app/generated/`` that contain a
``manifest.json`` (multi-EP mode), or ∅ with mode=single/absent.

* **absent** → no-op.
* **single** → require ``len(code) <= 1`` (name unconstrained).
* **multi** → require ``code == contract`` (exact set equality).

In all modes, unresolvable ``name=`` values produce an additional finding.
"""

from __future__ import annotations

import ast
from collections import Counter

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._code_entrypoints import CodeEntrypointScan
from ._contract_entrypoints import ContractEntrypointScan

_RULE_ID = "P016"


def _empty_directives() -> dict[int, _IgnoreDirective]:
    return {}


def _synthetic_node() -> ast.AST:
    """A dummy AST node at line 1 for cross-artifact findings with no code anchor."""
    node: ast.AST = ast.Constant(value=None)
    node.lineno = 1  # type: ignore[attr-defined]
    node.col_offset = 0  # type: ignore[attr-defined]
    return node


def _best_anchor(
    code: CodeEntrypointScan,
) -> tuple[str, ast.AST]:
    """Return ``(filename, node)`` to anchor a contract-only finding.

    Preference order:
    1. First ``App`` subclass found — the logical place to add a missing
       ``@entrypoint``.
    2. First existing ``@entrypoint`` — at least points into the right file.
    3. Synthetic node at line 1 of a fallback path — last resort.
    """
    if code.app_classes:
        loc = code.app_classes[0]
        return loc.filename, loc.node
    if code.entrypoints:
        ep = code.entrypoints[0]
        return ep.filename, ep.node
    return "app", _synthetic_node()


def check_p016(
    code: CodeEntrypointScan,
    contract: ContractEntrypointScan,
    directives_by_file: dict[str, dict[int, _IgnoreDirective]],
) -> list[Finding]:
    """Apply the P016 invariant and return the resulting findings.

    Parameters
    ----------
    code:
        Collected ``@entrypoint`` data from all Python source files.
    contract:
        Contract entry-point names derived from ``app/generated/``.
    directives_by_file:
        Parsed ``# conformance: ignore[...]`` directives, keyed by relative
        file path, so inline suppression works for code-side findings.
    """
    findings: list[Finding] = []

    # ── No-op when there is no contract to check against ─────────────────────
    if contract.mode == "absent":
        return findings

    # ── Unresolved name= (always emit, regardless of mode) ───────────────────
    for unresolved in code.unresolved:
        findings.append(
            make_finding(
                filename=unresolved.filename,
                rule_id=_RULE_ID,
                node=unresolved.node,
                message=(
                    "Entry point has a non-literal 'name=' argument — "
                    "static alignment check cannot be performed. "
                    "Use a string literal so the conformance suite can verify "
                    'it matches the contract: @entrypoint(name="<entry-point-name>"). '
                    "Suppress with '# conformance: ignore[P016] <reason>' if unavoidable."
                ),
                directives=directives_by_file.get(
                    unresolved.filename, _empty_directives()
                ),
            )
        )

    # ── Single-entry-point mode: require at most 1 @entrypoint in code ────────
    if contract.mode == "single":
        if len(code.entrypoints) > 1:
            # Anchor to the second entry point (the first extra one).
            extra = code.entrypoints[1]
            findings.append(
                make_finding(
                    filename=extra.filename,
                    rule_id=_RULE_ID,
                    node=extra.node,
                    message=(
                        f"Entry point '{extra.name}' is defined in code but the "
                        "contract is single-entry-point "
                        "(app/generated/manifest.json only, no per-entry-point subdirs). "
                        "Either add this entry point to contract/app.pkl as a named "
                        "Entrypoint block and re-run pkl eval, "
                        "or remove the extra @entrypoint from code."
                    ),
                    directives=directives_by_file.get(
                        extra.filename, _empty_directives()
                    ),
                )
            )
        return findings

    # ── Duplicate wire names in code (any mode) ──────────────────────────────
    # frozenset collapses duplicates silently; catch them here before the
    # set-equality check so two methods registering the same name both produce
    # a finding rather than neutralising each other.
    name_counts = Counter(ep.name for ep in code.entrypoints)
    for ep in code.entrypoints:
        if name_counts[ep.name] > 1:
            findings.append(
                make_finding(
                    filename=ep.filename,
                    rule_id=_RULE_ID,
                    node=ep.node,
                    message=(
                        f"Entry point '{ep.name}' is registered by more than one "
                        f"@entrypoint-decorated method ({name_counts[ep.name]} methods). "
                        "The SDK raises at registration time, but the duplicate must be "
                        "resolved: give each method a unique name= argument."
                    ),
                    directives=directives_by_file.get(ep.filename, _empty_directives()),
                )
            )
    if any(c > 1 for c in name_counts.values()):
        return findings

    # ── Multi-entry-point mode: exact set equality ────────────────────────────
    code_names = code.name_set()
    contract_names = contract.names
    contract_list = ", ".join(sorted(contract_names))

    # Code-only names: in @entrypoint code but absent from app/generated/
    for ep in code.entrypoints:
        if ep.name in contract_names:
            continue
        findings.append(
            make_finding(
                filename=ep.filename,
                rule_id=_RULE_ID,
                node=ep.node,
                message=(
                    f"Entry point '{ep.name}' is defined in code but not in the "
                    f"contract (contract defines: {contract_list}). "
                    f'Pin the name with @entrypoint(name="<contract-name>") to '
                    "match the contract, or update contract/app.pkl and re-run pkl eval. "
                    "Note: renaming is a breaking wire change "
                    "(workflow_type and ?entrypoint= value both change)."
                ),
                directives=directives_by_file.get(ep.filename, _empty_directives()),
            )
        )

    # Contract-only names: in app/generated/ but absent from @entrypoint code
    code_list = ", ".join(sorted(code_names)) or "(none)"
    anchor_file, anchor_node = _best_anchor(code)
    anchor_directives = directives_by_file.get(anchor_file, _empty_directives())

    for missing_name in sorted(contract_names - code_names):
        findings.append(
            make_finding(
                filename=anchor_file,
                rule_id=_RULE_ID,
                node=anchor_node,
                message=(
                    f"Entry point '{missing_name}' is defined in the contract "
                    f"(app/generated/{missing_name}/manifest.json) but not in code "
                    f"(code defines: {code_list}). "
                    f'Add @entrypoint(name="{missing_name}") to your App subclass, '
                    "or remove it from contract/app.pkl and re-run pkl eval."
                ),
                directives=anchor_directives,
            )
        )

    return findings
