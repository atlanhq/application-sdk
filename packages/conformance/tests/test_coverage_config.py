"""Meta-tests for the T014–T015 coverage-config integrity checks (BLDX-1400).

T014 catches a coverage gate that is configured but can never fail
(``fail_under`` absent or 0). T015 catches ``omit``/``source`` settings that
inflate the reported percentage by excluding real product code under
``app/`` from the denominator.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.checks.coverage_config import (
    _ci_declared_fail_under,
    scan_path,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.catalog import RemediationKind
from conformance.suite.schema.disposition import EnforcementTier, RuleScope

# ── Rule metadata ────────────────────────────────────────────────────────────


def test_t014_rule_metadata() -> None:
    rule = get_rule("T014")
    assert rule.name == "CoverageGateDisabled"
    assert rule.tier == EnforcementTier.WARN
    assert rule.scope == RuleScope.APP
    assert rule.category == "coverage-config"
    # Suggest-only (migration): restoring autofixable=True brings back unsafe lane edits.
    assert rule.autofixable is False
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.GUIDE


def test_t015_rule_metadata() -> None:
    rule = get_rule("T015")
    assert rule.name == "CoverageOmitsProductCode"
    assert rule.scope == RuleScope.APP
    assert rule.category == "coverage-config"
    # Suggest-only (migration): restoring autofixable=True brings back unsafe lane edits.
    assert rule.autofixable is False
    assert rule.remediation_reference is not None
    assert rule.remediation_reference.kind is RemediationKind.GUIDE


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


# ── T014: CI-declared fail-under cross-check ────────────────────────────────


def test_t014_silent_when_ci_fail_under_passed_directly() -> None:
    """pyproject has no fail_under, but the caller found a CI-declared floor."""
    findings = scan_text(
        "[tool.coverage.run]\nsource = ['app']\n",
        "pyproject.toml",
        ci_fail_under=60.0,
    )
    assert not any(f.rule_id == "T014" for f in findings)


def test_t014_fires_when_ci_fail_under_is_zero() -> None:
    """A workflow that explicitly declares a zero floor is still disabled."""
    findings = scan_text(
        "[tool.coverage.run]\nsource = ['app']\n\n"
        "[tool.coverage.report]\nfail_under = 85\n",
        "pyproject.toml",
        ci_fail_under=0.0,
    )
    assert "T014" in [f.rule_id for f in findings]


def test_t014_message_names_ci_zero_floor_and_unit_input() -> None:
    """A CI zero floor is named as the cause, and the fix points at the unit
    tier's input rather than at pyproject.toml or integration pytest-args."""
    findings = scan_text(
        "[tool.coverage.report]\nfail_under = 85\n",
        "pyproject.toml",
        ci_fail_under=0.0,
    )
    (t014,) = [f for f in findings if f.rule_id == "T014"]
    assert "CI coverage floor for the unit tier is 0" in t014.message
    assert 'unit-coverage-fail-under: "<N>"' in t014.message
    assert "leave --cov-fail-under=0 in integration pytest-args alone" in (t014.message)


def test_t014_message_without_ci_floor_names_pyproject() -> None:
    findings = scan_text("[tool.coverage.report]\nfail_under = 0\n", "pyproject.toml")
    (t014,) = [f for f in findings if f.rule_id == "T014"]
    assert "no CI workflow declares" in t014.message
    assert "unit tier is 0" not in t014.message


def test_ci_declared_fail_under_reads_connector_unit_tests_input(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "tests.yaml").write_text(
        "jobs:\n"
        "  unit:\n"
        "    uses: atlanhq/application-sdk/.github/actions/connector-unit-tests@main\n"
        "    with:\n"
        '      fail-under: "60"\n',
        encoding="utf-8",
    )
    assert _ci_declared_fail_under(tmp_path) == 60.0


_REUSABLE_USES = (
    "    uses: atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main\n"
)


def _write_workflow(root: Path, body: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "tests.yaml").write_text(body, encoding="utf-8")


def test_ci_declared_fail_under_reads_cov_fail_under_flag(tmp_path: Path) -> None:
    """A bespoke workflow's pytest-cov flag is its floor."""
    _write_workflow(
        tmp_path,
        "jobs:\n"
        "  unit:\n"
        "    steps:\n"
        "      - run: uv run pytest tests/unit --cov=app --cov-fail-under=60\n",
    )
    assert _ci_declared_fail_under(tmp_path) == 60.0


def test_ci_declared_fail_under_reads_reusable_unit_input(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "jobs:\n  tests:\n" + _REUSABLE_USES + "    with:\n"
        '      unit-coverage-fail-under: "90"\n',
    )
    assert _ci_declared_fail_under(tmp_path) == 90.0


def test_ci_declared_fail_under_reusable_ignores_integration_pytest_args(
    tmp_path: Path,
) -> None:
    """pytest-args reach only the integration tier, so --cov-fail-under there
    is not the unit floor; an omitted unit input inherits the reusable's 0."""
    _write_workflow(
        tmp_path,
        "jobs:\n  tests:\n" + _REUSABLE_USES + "    with:\n"
        "      pytest-args: >-\n"
        "        -n auto --cov=app --cov-report=xml --cov-fail-under=60\n",
    )
    assert _ci_declared_fail_under(tmp_path) == 0.0


def test_ci_declared_fail_under_reusable_unit_input_wins_regardless_of_order(
    tmp_path: Path,
) -> None:
    _write_workflow(
        tmp_path,
        "jobs:\n  tests:\n" + _REUSABLE_USES + "    with:\n"
        "      pytest-args: >-\n"
        "        --cov=app --cov-fail-under=0\n"
        '      unit-coverage-fail-under: "60"\n',
    )
    assert _ci_declared_fail_under(tmp_path) == 60.0


def test_ci_declared_fail_under_none_when_no_workflows(tmp_path: Path) -> None:
    assert _ci_declared_fail_under(tmp_path) is None


def test_scan_path_end_to_end_uses_ci_declared_floor(tmp_path: Path) -> None:
    """A bespoke workflow's `--cov-fail-under=60` suppresses T014 even with no
    pyproject fail_under."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.coverage.run]\nsource = ['app']\n", encoding="utf-8"
    )
    _write_workflow(
        tmp_path,
        "jobs:\n  tests:\n    steps:\n      - run: pytest --cov-fail-under=60\n",
    )
    findings = scan_path(tmp_path / "pyproject.toml", tmp_path)
    assert not any(f.rule_id == "T014" for f in findings)


def test_scan_path_reusable_integration_floor_does_not_hide_unit_gate(
    tmp_path: Path,
) -> None:
    """An integration-only floor in pytest-args, with no unit input and a
    positive TOML value, still fires: the unit tier is gated at 0."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.coverage.report]\nfail_under = 60\n", encoding="utf-8"
    )
    _write_workflow(
        tmp_path,
        "jobs:\n  tests:\n" + _REUSABLE_USES + "    with:\n"
        "      pytest-args: --cov-fail-under=60\n",
    )
    findings = scan_path(tmp_path / "pyproject.toml", tmp_path)
    assert "T014" in [f.rule_id for f in findings]


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
