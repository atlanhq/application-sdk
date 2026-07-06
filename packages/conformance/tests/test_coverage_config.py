"""Meta-tests for the T014–T015 coverage-config integrity checks (BLDX-1400).

T014 catches a coverage gate that is configured but can never fail
(``fail_under`` absent or 0). T015 catches ``omit``/``source`` settings that
inflate the reported percentage by excluding real product code under
``app/`` from the denominator.
"""

from __future__ import annotations

from conformance.suite.checks.coverage_config import scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t014_rule_metadata() -> None:
    rule = get_rule("T014")
    assert rule.name == "CoverageGateDisabled"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.category == "coverage-config"


def test_t015_rule_metadata() -> None:
    rule = get_rule("T015")
    assert rule.name == "CoverageOmitsProductCode"
    assert rule.scope == RuleScope.APP
    assert rule.category == "coverage-config"


# ── T014: coverage gate disabled ─────────────────────────────────────────────


def test_t014_fires_when_fail_under_is_zero() -> None:
    findings = scan_text(
        "[tool.coverage.run]\nsource = ['app']\n\n"
        "[tool.coverage.report]\nfail_under = 0\n",
        "pyproject.toml",
    )
    assert "T014" in [f.rule_id for f in findings]


def test_t014_fires_when_fail_under_absent() -> None:
    findings = scan_text("[tool.coverage.run]\nsource = ['app']\n", "pyproject.toml")
    assert "T014" in [f.rule_id for f in findings]


def test_t014_silent_when_fail_under_set() -> None:
    findings = scan_text(
        "[tool.coverage.run]\nsource = ['app']\n\n"
        "[tool.coverage.report]\nfail_under = 60\n",
        "pyproject.toml",
    )
    assert not any(f.rule_id == "T014" for f in findings)


def test_silent_when_no_coverage_config_at_all() -> None:
    findings = scan_text('[project]\nname = "x"\n', "pyproject.toml")
    assert findings == []


def test_t014_anchored_on_fail_under_line() -> None:
    findings = scan_text("[tool.coverage.report]\nfail_under = 0\n", "pyproject.toml")
    t014 = [f for f in findings if f.rule_id == "T014"]
    assert len(t014) == 1
    assert t014[0].line == 2  # the `fail_under = 0` line


def test_t014_suppressed_with_directive_above_key() -> None:
    findings = scan_text(
        "[tool.coverage.report]\n"
        "# conformance: ignore[T014] adoption in progress, tracked in BLDX-9999\n"
        "fail_under = 0\n",
        "pyproject.toml",
    )
    t014 = [f for f in findings if f.rule_id == "T014"]
    assert len(t014) == 1
    assert t014[0].suppressed is True


# ── T015: omit/source excludes product code ──────────────────────────────────


def test_t015_fires_on_omit_targeting_handlers() -> None:
    findings = scan_text(
        "[tool.coverage.run]\nomit = ['app/handlers/*']\n\n"
        "[tool.coverage.report]\nfail_under = 60\n",
        "pyproject.toml",
    )
    assert "T015" in [f.rule_id for f in findings]


def test_t015_silent_on_omit_targeting_generated_code() -> None:
    findings = scan_text(
        "[tool.coverage.run]\nomit = ['app/generated/*']\n\n"
        "[tool.coverage.report]\nfail_under = 60\n",
        "pyproject.toml",
    )
    assert not any(f.rule_id == "T015" for f in findings)


def test_t015_silent_on_omit_targeting_test_infra_under_app() -> None:
    findings = scan_text(
        "[tool.coverage.run]\nomit = ['app/**/test_*.py', 'app/**/conftest.py']\n\n"
        "[tool.coverage.report]\nfail_under = 60\n",
        "pyproject.toml",
    )
    assert not any(f.rule_id == "T015" for f in findings)


def test_t015_fires_when_source_excludes_app_entirely() -> None:
    findings = scan_text(
        "[tool.coverage.run]\nsource = ['lib']\n\n"
        "[tool.coverage.report]\nfail_under = 60\n",
        "pyproject.toml",
    )
    assert "T015" in [f.rule_id for f in findings]


def test_t015_silent_when_source_covers_app() -> None:
    findings = scan_text(
        "[tool.coverage.run]\nsource = ['app']\n\n"
        "[tool.coverage.report]\nfail_under = 60\n",
        "pyproject.toml",
    )
    assert not any(f.rule_id == "T015" for f in findings)


def test_t015_silent_when_source_absent() -> None:
    """An absent `source` key (coverage.py's own default) is not flagged."""
    findings = scan_text(
        "[tool.coverage.run]\nomit = ['app/generated/*']\n\n"
        "[tool.coverage.report]\nfail_under = 60\n",
        "pyproject.toml",
    )
    assert not any(f.rule_id == "T015" for f in findings)


def test_t015_suppressed_with_directive_above_omit() -> None:
    findings = scan_text(
        "[tool.coverage.run]\n"
        "# conformance: ignore[T015] vendored shim with no branch logic worth covering\n"
        "omit = ['app/vendor/*']\n\n"
        "[tool.coverage.report]\nfail_under = 60\n",
        "pyproject.toml",
    )
    t015 = [f for f in findings if f.rule_id == "T015"]
    assert len(t015) == 1
    assert t015[0].suppressed is True


def test_both_t014_and_t015_can_fire_together() -> None:
    findings = scan_text(
        "[tool.coverage.run]\nomit = ['app/handlers/*']\n\n"
        "[tool.coverage.report]\nfail_under = 0\n",
        "pyproject.toml",
    )
    assert sorted(f.rule_id for f in findings) == ["T014", "T015"]
