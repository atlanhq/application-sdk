"""P025 check implementation — apply the app-name alignment invariant.

Invariant
---------
Let ``code_name`` = the single resolved name from the leaf App-family class
(or ``None`` when ambiguous / unresolvable).
Let ``contract_name`` = the name from ``atlan.yaml`` / ``manifest.json`` fallback
(or ``None`` when absent).
Let ``env_name`` = ``ATLAN_APPLICATION_NAME`` from ``.env.example`` (or ``None``
when absent).

The **code name is authoritative** (mirrors ``AppRegistry.resolve_running_app_name``
from PR #2380).  A finding is emitted when:

* **Unresolvable** — a leaf App-family class has a non-literal ``name = ...``
  attribute.  Emitted regardless of whether any other source is present.
* **Contract drift** — ``code_name != contract_name`` (and both are present).
* **Env drift** — ``code_name != env_name`` (and both are present).

No-ops
------
* No leaf App-family class found in code (SDK repos, utility libraries).
* No contract and no env present (non-native-app repos).
* Multiple leaf App-family classes with *different* resolved names (bundle repos
  where P016 already covers entrypoint-name alignment).
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._code_app_name import AppClassInfo, CodeAppNameScan
from ._contract_app_name import ContractAppNameScan

_RULE_ID = "P025"


def _empty_directives() -> dict[int, _IgnoreDirective]:
    return {}


def _synthetic_node(line: int = 1) -> ast.AST:
    """A dummy AST node at *line* for findings with no natural code anchor."""
    node: ast.AST = ast.Constant(value=None)
    node.lineno = line  # type: ignore[attr-defined]
    node.col_offset = 0  # type: ignore[attr-defined]
    return node


def _best_anchor(code: CodeAppNameScan) -> tuple[str, ast.AST]:
    """Return ``(filename, node)`` for a finding with no natural code anchor.

    Falls back through: first resolved leaf → first unresolvable leaf →
    synthetic node at app/connector.py:1.
    """
    if code.resolved:
        info = code.resolved[0]
        return info.filename, info.node
    if code.unresolvable:
        info = code.unresolvable[0]
        return info.filename, info.node
    return "app/connector.py", _synthetic_node()


def check_p025(
    code: CodeAppNameScan,
    contract: ContractAppNameScan,
    directives_by_file: dict[str, dict[int, _IgnoreDirective]],
) -> list[Finding]:
    """Apply the P025 invariant and return all resulting findings.

    Parameters
    ----------
    code:
        Accumulated App-family leaf class scan from all Python source files.
    contract:
        App name values read from ``atlan.yaml`` / manifest fallback and
        ``.env.example``.
    directives_by_file:
        Parsed ``# conformance: ignore[...]`` directives keyed by repo-relative
        path, enabling inline suppression on App class definition lines.
    """
    findings: list[Finding] = []

    # ── Unresolvable name= (always emit) ─────────────────────────────────────
    for info in code.unresolvable:
        findings.append(
            make_finding(
                filename=info.filename,
                rule_id=_RULE_ID,
                node=info.node,
                message=(
                    f"App class '{info.class_name}' has a non-literal 'name = ...' "
                    "attribute — the app name cannot be statically verified for "
                    "alignment with atlan.yaml or ATLAN_APPLICATION_NAME. "
                    'Use a string literal: name = "<intended-name>". '
                    "Suppress with '# conformance: ignore[P025] <reason>' if unavoidable."
                ),
                directives=directives_by_file.get(info.filename, _empty_directives()),
            )
        )

    # ── Determine the single reference code name ──────────────────────────────
    # Resolve a single canonical code name from the leaf classes.
    # No-op (comparison only) when:
    #   * No resolved leaves (nothing to check against)
    #   * Multiple leaves with *different* names (bundle — ambiguous reference)
    code_name: str | None = None
    reference_info: AppClassInfo | None = None

    if code.resolved:
        unique_names = {info.resolved_name for info in code.resolved}
        if len(unique_names) == 1:
            code_name = next(iter(unique_names))
            reference_info = code.resolved[0]
        # else: multiple different names → bundle scenario → skip comparison

    # ── No other source to compare against → no-op ───────────────────────────
    if contract.contract_name is None and contract.env_name is None:
        return findings  # Not a native-app-contract repo.

    if code_name is None:
        # No unique code reference — skip contract/env comparison.
        return findings

    assert reference_info is not None  # implied by code_name is not None

    # ── Contract drift ────────────────────────────────────────────────────────
    if contract.contract_name is not None and code_name != contract.contract_name:
        source = contract.contract_source or "atlan.yaml"
        findings.append(
            make_finding(
                filename=reference_info.filename,
                rule_id=_RULE_ID,
                node=reference_info.node,
                message=(
                    f"App name '{code_name}' (code) does not match "
                    f"'{contract.contract_name}' in {source}. "
                    "The code-derived name is authoritative (it drives artifact "
                    "upload paths via App.upload()). "
                    "Fix one of: "
                    f'(1) add name = "{contract.contract_name}" to the App '
                    "subclass if the contract name is intended; "
                    f'(2) update contract/app.pkl name to "{code_name}" and '
                    "re-run pkl eval so atlan.yaml follows (never hand-edit "
                    "atlan.yaml — C002 catches stale generated artifacts). "
                    "Suppress with '# conformance: ignore[P025] <reason>' "
                    "on the class definition line when a deliberate mismatch "
                    "is justified."
                ),
                directives=directives_by_file.get(
                    reference_info.filename, _empty_directives()
                ),
            )
        )

    # ── Env drift ─────────────────────────────────────────────────────────────
    if contract.env_name is not None and code_name != contract.env_name:
        findings.append(
            make_finding(
                filename=reference_info.filename,
                rule_id=_RULE_ID,
                node=reference_info.node,
                message=(
                    f"App name '{code_name}' (code) does not match "
                    f"ATLAN_APPLICATION_NAME='{contract.env_name}' in "
                    ".env.example. "
                    "The code-derived name is authoritative. "
                    "Fix one of: "
                    f'(1) add name = "{contract.env_name}" to the App subclass '
                    "if the env value is intended; "
                    f'(2) update ATLAN_APPLICATION_NAME to "{code_name}" in '
                    ".env.example. "
                    "Suppress with '# conformance: ignore[P025] <reason>' "
                    "on the class definition line when a deliberate mismatch "
                    "is justified."
                ),
                directives=directives_by_file.get(
                    reference_info.filename, _empty_directives()
                ),
            )
        )

    return findings
