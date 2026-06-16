"""Tests for C002 BootstrapWorkflowDrift check.

Covers:
- Clean (freshly bootstrapped) repo → no findings
- Missing managed workflow → one C002 WARN finding per absent file
- Byte-drifted managed workflow → one C002 WARN finding
- Parameterised files with a custom-but-valid value → no finding (structural match)
- Parameterised files with structural drift beyond the value → finding
"""

from __future__ import annotations

import pathlib

import pytest
from conformance.bootstrap.render import MANAGED_WORKFLOWS, render
from conformance.cli import _cmd_bootstrap
from conformance.suite.checks.bootstrap_drift import discover, scan_path
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bootstrap(root: pathlib.Path) -> None:
    """Run bootstrap in *root* (no chdir needed — uses monkeypatch elsewhere)."""
    import os

    old = os.getcwd()
    os.chdir(root)
    try:
        _cmd_bootstrap([])
    finally:
        os.chdir(old)


# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_c002_rule_exists() -> None:
    rule = get_rule("C002")
    assert rule.name == "BootstrapWorkflowDrift"


def test_c002_tier_is_warn() -> None:
    rule = get_rule("C002")
    assert rule.tier == EnforcementTier.WARN


def test_c002_is_autofixable() -> None:
    assert get_rule("C002").autofixable is True


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_returns_all_managed_paths(tmp_path: pathlib.Path) -> None:
    paths = discover(tmp_path)
    names = {p.name for p in paths}
    assert names == set(MANAGED_WORKFLOWS)


def test_discover_returns_paths_even_when_absent(tmp_path: pathlib.Path) -> None:
    """discover() must not filter out non-existent files."""
    paths = discover(tmp_path)
    assert len(paths) == len(MANAGED_WORKFLOWS)
    # None of them exist yet.
    assert all(not p.exists() for p in paths)


# ---------------------------------------------------------------------------
# Clean repo — no findings
# ---------------------------------------------------------------------------


def test_clean_bootstrapped_repo_has_no_c002_findings(
    tmp_path: pathlib.Path,
) -> None:
    _bootstrap(tmp_path)
    all_findings = []
    for path in discover(tmp_path):
        all_findings.extend(scan_path(path, tmp_path))
    assert all_findings == [], [f.message for f in all_findings]


# ---------------------------------------------------------------------------
# Missing file → finding
# ---------------------------------------------------------------------------


def test_missing_workflow_produces_finding(tmp_path: pathlib.Path) -> None:
    # Don't bootstrap — the directory doesn't even exist yet.
    wf_path = tmp_path / ".github" / "workflows" / "conformance.yaml"
    findings = scan_path(wf_path, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"
    assert "absent" in findings[0].message


def test_missing_workflow_finding_names_the_file(tmp_path: pathlib.Path) -> None:
    wf_path = tmp_path / ".github" / "workflows" / "checks.yml"
    findings = scan_path(wf_path, tmp_path)
    assert "checks.yml" in findings[0].message


def test_missing_workflow_finding_mentions_bootstrap_command(
    tmp_path: pathlib.Path,
) -> None:
    wf_path = tmp_path / ".github" / "workflows" / "stale.yml"
    findings = scan_path(wf_path, tmp_path)
    assert "bootstrap" in findings[0].message


# ---------------------------------------------------------------------------
# Drifted file → finding
# ---------------------------------------------------------------------------


def test_drifted_workflow_produces_finding(tmp_path: pathlib.Path) -> None:
    _bootstrap(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "conformance.yaml"
    wf.write_text(wf.read_text() + "\n# extra line injected by drift\n")
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"
    assert "drifted" in findings[0].message


def test_drifted_workflow_finding_names_the_file(tmp_path: pathlib.Path) -> None:
    _bootstrap(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "commits.yaml"
    wf.write_text("completely wrong content")
    findings = scan_path(wf, tmp_path)
    assert "commits.yaml" in findings[0].message


# ---------------------------------------------------------------------------
# Parameterised files: custom value → no structural finding
# ---------------------------------------------------------------------------


def test_docstring_coverage_custom_package_name_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """A repo that used --package-name myapp should not be flagged."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "docstring-coverage.yaml"
    wf.write_text(render("docstring-coverage.yaml", package_name="myapp"))
    findings = scan_path(wf, tmp_path)
    assert findings == []


def test_build_and_publish_custom_unit_tests_workflow_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """A repo that used --unit-tests-workflow ci.yaml should not be flagged."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "build-and-publish.yaml"
    wf.write_text(render("build-and-publish.yaml", unit_tests_workflow="ci.yaml"))
    findings = scan_path(wf, tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# Parameterised files: structural drift beyond the value → finding
# ---------------------------------------------------------------------------


def test_docstring_coverage_structural_drift_flagged(tmp_path: pathlib.Path) -> None:
    """Structural change (not just the value) in a templated file is still flagged."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "docstring-coverage.yaml"
    # Write canonical then inject an extra job-level key.
    canonical = render("docstring-coverage.yaml")
    drifted = canonical.replace(
        "  docstring-coverage:", "  docstring-coverage:\n    timeout-minutes: 5"
    )
    wf.write_text(drifted)
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert "drifted" in findings[0].message


# ---------------------------------------------------------------------------
# All managed workflows: end-to-end sweep on a bootstrapped repo
# ---------------------------------------------------------------------------


def test_all_managed_workflows_clean_after_bootstrap(
    tmp_path: pathlib.Path,
) -> None:
    _bootstrap(tmp_path)
    findings = []
    for path in discover(tmp_path):
        findings.extend(scan_path(path, tmp_path))
    assert findings == [], [f.message for f in findings]


@pytest.mark.parametrize("name", MANAGED_WORKFLOWS)
def test_each_workflow_clean_individually(tmp_path: pathlib.Path, name: str) -> None:
    _bootstrap(tmp_path)
    wf = tmp_path / ".github" / "workflows" / name
    findings = scan_path(wf, tmp_path)
    assert findings == [], f"{name}: {[f.message for f in findings]}"
