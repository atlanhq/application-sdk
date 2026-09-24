"""Tests for K024/K025/K026 contract-hygiene rules on ``contract/app.pkl``.

* K024 fires when ``metadata`` or ``atlanYamlOverrides`` sets a key App.pkl
  already models as a typed field.
* K025 fires when a String field whose default is ``""`` is assigned ``""``.
* K026 fires when a field App.pkl marks ``@Deprecated`` is assigned.

Test helpers
------------
``_app_repo``: writes ``contract/app.pkl`` under ``tmp_path`` and returns the
``scan_all`` findings for it.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.contract_hygiene import discover, scan_all
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope
from conformance.suite.schema.findings import Finding

_CLEAN = (
    'amends "@app-contract-toolkit/App.pkl"\n'
    "\n"
    'name = "metabase"\n'
    'icon = "https://assets.atlan.com/assets/metabase.svg"\n'
    'shortDescription = "Crawl Metabase collections"\n'
)

_HATCH_UNTYPED_ONLY = _CLEAN + (
    "atlanYamlOverrides {\n"
    '  ["dockerfile"] = "./Dockerfile"\n'
    '  ["release_model"] = "semver"\n'
    "}\n"
)

_HATCH_SHADOWS_TYPED = _CLEAN + (
    "metadata {\n"
    '  ["release_model"] = "semver"\n'
    '  ["display_name"] = "Metabase Assets"\n'
    "}\n"
)

_HATCH_SHADOWS_DEPLOY = _CLEAN + (
    "atlanYamlOverrides {\n"
    '  ["deploy"] = new Mapping {\n'
    '    ["execution_mode"] = "native"\n'
    "  }\n"
    "}\n"
)

_BLANK_DOCS_URL = _CLEAN + 'docsUrl = ""\n'
_BLANK_TWO_FIELDS = _CLEAN + 'docsUrl = ""\nhelpdeskLink = ""\n'
_DEPRECATED_FIELD = _CLEAN + "emitEntrypoints = false\n"

_COMMENTED_OUT = _CLEAN + ('// docsUrl = ""\n// emitEntrypoints = false\n')


def _app_repo(tmp_path: Path, *, contract: str | None = None) -> list[Finding]:
    """Write ``contract/app.pkl`` under *tmp_path* and return its findings."""
    if contract is not None:
        (tmp_path / "contract").mkdir()
        (tmp_path / "contract" / "app.pkl").write_text(contract, encoding="utf-8")
    return scan_all(discover(tmp_path), tmp_path)


def _ids(findings: list[Finding]) -> list[str]:
    """Rule IDs of unsuppressed findings (matching runner gate semantics)."""
    return [f.rule_id for f in findings if not f.suppressed]


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_k024_rule_metadata() -> None:
    rule = get_rule("K024")
    assert rule.name == "EscapeHatchShadowsTypedField"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP


def test_k025_rule_metadata() -> None:
    rule = get_rule("K025")
    assert rule.name == "BlankStringAssignment"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP


def test_k026_rule_metadata() -> None:
    rule = get_rule("K026")
    assert rule.name == "DeprecatedContractField"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP


# ---------------------------------------------------------------------------
# discover gating
# ---------------------------------------------------------------------------


def test_no_contract_file_is_not_an_app_repo(tmp_path: Path) -> None:
    assert discover(tmp_path) == []
    assert _app_repo(tmp_path) == []


def test_clean_contract_is_silent(tmp_path: Path) -> None:
    assert _ids(_app_repo(tmp_path, contract=_CLEAN)) == []


# ---------------------------------------------------------------------------
# K024 — escape hatch shadowing a typed field
# ---------------------------------------------------------------------------


def test_k024_silent_for_untyped_keys(tmp_path: Path) -> None:
    assert "K024" not in _ids(_app_repo(tmp_path, contract=_HATCH_UNTYPED_ONLY))


def test_k024_fires_on_typed_key_in_metadata(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, contract=_HATCH_SHADOWS_TYPED)
    assert _ids(findings).count("K024") == 1
    assert "display_name" in findings[0].message


def test_k024_fires_on_deploy_in_overrides(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, contract=_HATCH_SHADOWS_DEPLOY)
    assert "K024" in _ids(findings)
    assert "deploy" in findings[0].message


# ---------------------------------------------------------------------------
# K025 — blank string assignment
# ---------------------------------------------------------------------------


def test_k025_fires_on_blank_docs_url(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, contract=_BLANK_DOCS_URL)
    assert _ids(findings) == ["K025"]
    assert "docsUrl" in findings[0].message


def test_k025_fires_once_per_field(tmp_path: Path) -> None:
    assert _ids(_app_repo(tmp_path, contract=_BLANK_TWO_FIELDS)).count("K025") == 2


def test_k025_silent_when_field_has_a_value(tmp_path: Path) -> None:
    assert "K025" not in _ids(_app_repo(tmp_path, contract=_CLEAN))


# ---------------------------------------------------------------------------
# K026 — deprecated contract field
# ---------------------------------------------------------------------------


def test_k026_fires_on_emit_entrypoints(tmp_path: Path) -> None:
    findings = _app_repo(tmp_path, contract=_DEPRECATED_FIELD)
    assert _ids(findings) == ["K026"]
    assert "emitEntrypoints" in findings[0].message


# ---------------------------------------------------------------------------
# Comment handling + suppression
# ---------------------------------------------------------------------------


def test_commented_assignments_do_not_fire(tmp_path: Path) -> None:
    assert _ids(_app_repo(tmp_path, contract=_COMMENTED_OUT)) == []


def test_directive_suppresses_finding(tmp_path: Path) -> None:
    contract = _CLEAN + (
        "// conformance: ignore[K026] bundle migration tracked in FND-0000\n"
        "emitEntrypoints = false\n"
    )
    assert "K026" not in _ids(_app_repo(tmp_path, contract=contract))


def test_directive_does_not_reach_a_distant_finding(tmp_path: Path) -> None:
    contract = (
        "// conformance: ignore[K026] directive far above the assignment\n"
        + _DEPRECATED_FIELD
    )
    assert "K026" in _ids(_app_repo(tmp_path, contract=contract))
