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
import re

import pytest
from conformance.bootstrap.render import MANAGED_ACTION_FILES, MANAGED_WORKFLOWS, render
from conformance.cli import _cmd_bootstrap
from conformance.suite.checks.bootstrap_drift import (
    _extract_exit_zero,
    discover,
    scan_path,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bootstrap(root: pathlib.Path, *argv: str) -> None:
    """Run bootstrap in *root* with optional flags (e.g. ``"--enforce", "false"``)."""
    import os

    old = os.getcwd()
    os.chdir(root)
    try:
        _cmd_bootstrap(list(argv))
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


def test_c002_orthogonal_gate_is_skip() -> None:
    assert get_rule("C002").orthogonal_gate == "skip"


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_returns_all_managed_paths(tmp_path: pathlib.Path) -> None:
    paths = discover(tmp_path)
    names = {p.name for p in paths}
    rels = {p.relative_to(tmp_path).as_posix() for p in paths}
    # Must include all managed shims plus the write-if-absent scaffolds.
    assert set(MANAGED_WORKFLOWS).issubset(names)
    assert "tests.yaml" in names
    assert "renovate.json" in names
    # And the vendored non-workflow files (action.yaml + arg-building script).
    for dest_rel, _template_name in MANAGED_ACTION_FILES:
        assert dest_rel in rels


def test_discover_returns_paths_even_when_absent(tmp_path: pathlib.Path) -> None:
    """discover() must not filter out non-existent files."""
    paths = discover(tmp_path)
    # managed shims + managed action files + tests.yaml + renovate.json scaffolds.
    assert len(paths) == len(MANAGED_WORKFLOWS) + len(MANAGED_ACTION_FILES) + 2
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


@pytest.mark.parametrize("dest_rel", [d for d, _ in MANAGED_ACTION_FILES])
def test_missing_managed_action_file_produces_finding(
    tmp_path: pathlib.Path, dest_rel: str
) -> None:
    # Don't bootstrap — the vendored action/script isn't on disk at all.
    path = tmp_path / dest_rel
    findings = scan_path(path, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"
    assert "absent" in findings[0].message
    assert dest_rel in findings[0].message


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


@pytest.mark.parametrize("dest_rel", [d for d, _ in MANAGED_ACTION_FILES])
def test_drifted_managed_action_file_produces_finding(
    tmp_path: pathlib.Path, dest_rel: str
) -> None:
    _bootstrap(tmp_path)
    path = tmp_path / dest_rel
    path.write_text("completely wrong content")
    findings = scan_path(path, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"
    assert "drifted" in findings[0].message
    assert dest_rel in findings[0].message


# ---------------------------------------------------------------------------
# Action pin bumps — ignored during drift comparison
# ---------------------------------------------------------------------------

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def test_pin_only_change_not_flagged(tmp_path: pathlib.Path) -> None:
    """Automations may bump SHA pins freely — a pin-only diff must not flag C002."""
    _bootstrap(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "checks.yml"
    bumped = re.sub(r"@[0-9a-f]{40}", f"@{_SHA_B}", wf.read_text())
    wf.write_text(bumped)
    assert scan_path(wf, tmp_path) == []


def test_pin_and_comment_change_not_flagged(tmp_path: pathlib.Path) -> None:
    """Renovate typically updates both the SHA and the adjacent version comment."""
    _bootstrap(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "checks.yml"
    bumped = re.sub(
        r"@[0-9a-f]{40}(?:[ \t]+#[^\n]*)?", f"@{_SHA_B} # v99", wf.read_text()
    )
    wf.write_text(bumped)
    assert scan_path(wf, tmp_path) == []


def test_structural_change_alongside_pin_still_flagged(tmp_path: pathlib.Path) -> None:
    """A structural edit is caught even when the SHA pin was also bumped."""
    _bootstrap(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "checks.yml"
    bumped = re.sub(r"@[0-9a-f]{40}", f"@{_SHA_B}", wf.read_text())
    drifted = bumped + "\n# injected structural drift\n"
    wf.write_text(drifted)
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


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


def _renovate_pkl_sync_with_regen(value: str) -> str:
    """Canonical caller with a `with: regenerate-contract: <value>` override."""
    canonical = render("renovate-pkl-sync.yaml")
    return canonical.replace(
        "    secrets:",
        f"    with:\n      regenerate-contract: {value}\n    secrets:",
        1,
    )


def test_renovate_pkl_sync_optout_not_flagged(tmp_path: pathlib.Path) -> None:
    """Opting out of regeneration (regenerate-contract: false) is a sanctioned
    per-repo choice, not drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "renovate-pkl-sync.yaml"
    wf.write_text(_renovate_pkl_sync_with_regen("false"))
    assert scan_path(wf, tmp_path) == []


def test_renovate_pkl_sync_explicit_true_not_flagged(tmp_path: pathlib.Path) -> None:
    """An explicit regenerate-contract: true (the default, stated redundantly)
    is also fine."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "renovate-pkl-sync.yaml"
    wf.write_text(_renovate_pkl_sync_with_regen("true"))
    assert scan_path(wf, tmp_path) == []


def test_renovate_pkl_sync_other_drift_still_flagged(tmp_path: pathlib.Path) -> None:
    """Stripping the regenerate-contract override must not mask real drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "renovate-pkl-sync.yaml"
    drifted = _renovate_pkl_sync_with_regen("false").replace("@main", "@v1")
    wf.write_text(drifted)
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


def test_renovate_pkl_sync_extra_with_key_still_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """The strip is targeted: only `regenerate-contract` is sanctioned. A second
    key under `with:` is unsanctioned drift and must still flag."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "renovate-pkl-sync.yaml"
    canonical = render("renovate-pkl-sync.yaml")
    drifted = canonical.replace(
        "    secrets:",
        "    with:\n      regenerate-contract: false\n      some-other-key: true\n    secrets:",
        1,
    )
    wf.write_text(drifted)
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


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


# ---------------------------------------------------------------------------
# tests.yaml scaffold — write-if-absent, WARN-only drift tracking
# ---------------------------------------------------------------------------


def test_tests_yaml_clean_after_bootstrap(tmp_path: pathlib.Path) -> None:
    """A freshly-bootstrapped tests.yaml yields no C002 finding."""
    _bootstrap(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    assert wf.exists()
    findings = scan_path(wf, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_tests_yaml_custom_app_name_not_flagged(tmp_path: pathlib.Path) -> None:
    """Custom app-name is a recognised param — not structural drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(render("tests.yaml", app_name="mysql"))
    findings = scan_path(wf, tmp_path)
    assert findings == []


def test_tests_yaml_custom_enable_e2e_not_flagged(tmp_path: pathlib.Path) -> None:
    """Custom enable-e2e value is a recognised param — not structural drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(render("tests.yaml", app_name="hello-world", enable_e2e="false"))
    findings = scan_path(wf, tmp_path)
    assert findings == []


def test_tests_yaml_active_services_script_not_flagged(tmp_path: pathlib.Path) -> None:
    """Uncommented services-script is a recognised param — not structural drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(
        render(
            "tests.yaml",
            app_name="openapi",
            services_script=".github/test/setup-services.sh",
        )
    )
    findings = scan_path(wf, tmp_path)
    assert findings == []


def _opt_into_parallel(
    canonical: str,
    value: str,
    *,
    comment: bool = False,
    trailing_comment: bool = False,
) -> str:
    """Insert an e2e-parallel-workers input into the canonical tests.yaml."""
    line = f'      e2e-parallel-workers: "{value}"'
    if trailing_comment:
        line += "  # tune to the runner's core count"
    block = line + "\n"
    if comment:
        block = "      # run independent e2e classes concurrently\n" + block
    return canonical.replace(
        "      two-store: true\n", "      two-store: true\n" + block
    )


def test_tests_yaml_e2e_parallel_workers_not_flagged(tmp_path: pathlib.Path) -> None:
    """Opting the e2e job into pytest-xdist (e2e-parallel-workers) is a
    sanctioned per-repo choice — not structural drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(_opt_into_parallel(render("tests.yaml"), "2"))
    findings = scan_path(wf, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_tests_yaml_e2e_parallel_workers_auto_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """The 'auto' worker count is also sanctioned."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(_opt_into_parallel(render("tests.yaml"), "auto"))
    assert scan_path(wf, tmp_path) == []


def test_tests_yaml_e2e_parallel_workers_with_comment_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """A single explanatory comment directly above the input is stripped too."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(_opt_into_parallel(render("tests.yaml"), "2", comment=True))
    assert scan_path(wf, tmp_path) == []


@pytest.mark.parametrize("bad_value", ["0", "01", "-1", "two"])
def test_tests_yaml_invalid_e2e_parallel_workers_still_flagged(
    tmp_path: pathlib.Path, bad_value: str
) -> None:
    """Only values the runtime driver accepts (auto / positive int, no leading
    zero) are sanctioned. An invalid value flags drift so conformance catches
    the typo before the driver rejects it at e2e runtime."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(_opt_into_parallel(render("tests.yaml"), bad_value))
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


def test_tests_yaml_e2e_parallel_workers_trailing_comment_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """A same-line trailing comment after the input is normalised out too, so it
    doesn't orphan and flag spurious drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(_opt_into_parallel(render("tests.yaml"), "2", trailing_comment=True))
    assert scan_path(wf, tmp_path) == []


def test_tests_yaml_e2e_parallel_workers_does_not_mask_other_drift(
    tmp_path: pathlib.Path,
) -> None:
    """The strip is targeted: real structural drift alongside the sanctioned
    e2e-parallel-workers input is still flagged."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    opted_in = _opt_into_parallel(render("tests.yaml"), "2")
    drifted = opted_in.replace(
        "  tests:", "  tests:\n    timeout-minutes: 999  # structural drift"
    )
    wf.write_text(drifted)
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


def test_tests_yaml_structural_drift_produces_finding(tmp_path: pathlib.Path) -> None:
    """Structural modification (not just param values) → one C002 finding."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    canonical = render("tests.yaml")
    drifted = canonical.replace(
        "  tests:", "  tests:\n    timeout-minutes: 999  # structural drift"
    )
    wf.write_text(drifted)
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"
    assert "drifted" in findings[0].message


def test_tests_yaml_missing_produces_finding(tmp_path: pathlib.Path) -> None:
    """Absent tests.yaml → one C002 finding."""
    wf_path = tmp_path / ".github" / "workflows" / "tests.yaml"
    findings = scan_path(wf_path, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


def test_tests_yaml_finding_is_warn_never_block(tmp_path: pathlib.Path) -> None:
    """tests.yaml drift finding is WARN, not BLOCK — drift must never block CI."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text("completely wrong content")
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    # C002 is defined as WARN tier — the finding inherits that.
    rule = get_rule("C002")
    assert rule.tier == EnforcementTier.WARN


def test_tests_yaml_finding_message_mentions_delete_and_bootstrap(
    tmp_path: pathlib.Path,
) -> None:
    """Drift message tells users to delete + re-run bootstrap to remediate."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text("structural drift")
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    msg = findings[0].message
    assert "delete" in msg.lower() or "regenerate" in msg.lower()
    assert "bootstrap" in msg


# ---------------------------------------------------------------------------
# renovate.json scaffold — write-if-absent, WARN-only drift tracking
# ---------------------------------------------------------------------------


def test_renovate_json_clean_after_bootstrap(tmp_path: pathlib.Path) -> None:
    """A freshly-bootstrapped renovate.json yields no C002 finding."""
    _bootstrap(tmp_path)
    rj = tmp_path / "renovate.json"
    assert rj.exists()
    findings = scan_path(rj, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_renovate_json_missing_produces_finding(tmp_path: pathlib.Path) -> None:
    """Absent renovate.json → one C002 finding."""
    rj = tmp_path / "renovate.json"
    findings = scan_path(rj, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"
    assert "absent" in findings[0].message


def test_renovate_json_drifted_produces_finding(tmp_path: pathlib.Path) -> None:
    """Structurally modified renovate.json → one C002 WARN finding."""
    _bootstrap(tmp_path)
    rj = tmp_path / "renovate.json"
    rj.write_text('{"completely": "wrong"}\n')
    findings = scan_path(rj, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"
    assert "drifted" in findings[0].message


def test_renovate_json_finding_is_warn_never_block(tmp_path: pathlib.Path) -> None:
    """renovate.json drift is WARN, not BLOCK."""
    rj = tmp_path / "renovate.json"
    rj.write_text("{}\n")
    findings = scan_path(rj, tmp_path)
    assert len(findings) == 1
    assert get_rule("C002").tier == EnforcementTier.WARN


def test_renovate_json_finding_mentions_delete_and_bootstrap(
    tmp_path: pathlib.Path,
) -> None:
    """Drift message tells users to delete + re-run bootstrap to remediate."""
    rj = tmp_path / "renovate.json"
    rj.write_text("{}\n")
    findings = scan_path(rj, tmp_path)
    assert len(findings) == 1
    msg = findings[0].message
    assert "delete" in msg.lower() or "regenerate" in msg.lower()
    assert "bootstrap" in msg


def test_all_scaffolds_clean_after_bootstrap(tmp_path: pathlib.Path) -> None:
    """Every discovered path — managed shims + both scaffolds — is clean after bootstrap."""
    _bootstrap(tmp_path)
    findings = []
    for path in discover(tmp_path):
        findings.extend(scan_path(path, tmp_path))
    assert findings == [], [f.message for f in findings]


# ---------------------------------------------------------------------------
# Soft-mode (--enforce false) repos: exit-zero/automerge must not be flagged
# ---------------------------------------------------------------------------


def test_soft_mode_conformance_yaml_not_flagged(tmp_path: pathlib.Path) -> None:
    """A repo bootstrapped with --enforce false must not show C002 drift on
    conformance.yaml — its exit-zero mode is a recognised param, not drift."""
    _bootstrap(tmp_path, "--enforce", "false")
    wf = tmp_path / ".github" / "workflows" / "conformance.yaml"
    findings = scan_path(wf, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_soft_mode_renovate_json_not_flagged(tmp_path: pathlib.Path) -> None:
    """A repo bootstrapped with --enforce false must not show C002 drift on
    renovate.json — its soft-rollout block is a recognised param, not drift."""
    _bootstrap(tmp_path, "--enforce", "false")
    rj = tmp_path / "renovate.json"
    findings = scan_path(rj, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_soft_mode_survives_bare_rerun_without_drift(tmp_path: pathlib.Path) -> None:
    """A bare re-run (no --enforce) after an explicit soft-mode bootstrap must
    preserve soft mode (per the bootstrap auto-detection) *and* stay clean of
    C002 findings for both conformance.yaml and renovate.json."""
    _bootstrap(tmp_path, "--enforce", "false")
    _bootstrap(tmp_path)  # bare re-run
    wf = tmp_path / ".github" / "workflows" / "conformance.yaml"
    rj = tmp_path / "renovate.json"
    findings = scan_path(wf, tmp_path) + scan_path(rj, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_conformance_yaml_structural_drift_still_flagged_in_soft_mode(
    tmp_path: pathlib.Path,
) -> None:
    """Extracting exit-zero must not mask a genuine structural change."""
    _bootstrap(tmp_path, "--enforce", "false")
    wf = tmp_path / ".github" / "workflows" / "conformance.yaml"
    wf.write_text(wf.read_text() + "\n# structural drift\n")
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


def test_renovate_json_structural_drift_still_flagged_in_soft_mode(
    tmp_path: pathlib.Path,
) -> None:
    """Extracting automerge must not mask a genuine structural change."""
    _bootstrap(tmp_path, "--enforce", "false")
    rj = tmp_path / "renovate.json"
    rj.write_text(rj.read_text().replace("platformAutomerge", "platform_automerge"))
    findings = scan_path(rj, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


# ---------------------------------------------------------------------------
# _extract_exit_zero — unparseable exit-zero line falls back to renovate.json
#
# Unit-tested directly (rather than through scan_path/discover) because the
# fallback's effect is invisible at the scan_path level: conformance.yaml's
# exit-zero line is the *only* place `exit_zero` is substituted (verified
# against the template), so an unparseable line is always textually
# different from any correctly-rendered canonical line regardless of which
# exit_zero value the extractor falls back to -- scan_path always reports
# drift for that file either way, correctly. What the fallback actually
# fixes is `kwargs["exit_zero"]` itself matching bootstrap's own
# `_read_conformance_enforce` autodetection (see autodetect.py) instead of
# silently defaulting to hard-gate, so the two callers of `extract.py` can't
# quietly disagree about a repo's inferred enforcement mode.
# ---------------------------------------------------------------------------


def test_extract_exit_zero_unparseable_falls_back_to_renovate_soft_mode(
    tmp_path: pathlib.Path,
) -> None:
    """An unparseable exit-zero line with a soft-mode renovate.json falls back
    to "true" (soft/observe), matching autodetect._read_conformance_enforce's
    fallback instead of silently defaulting to hard-gate."""
    _bootstrap(tmp_path, "--enforce", "false")  # writes a soft-mode renovate.json
    assert _extract_exit_zero("exit-zero: not-a-recognised-expression", tmp_path) == (
        "true"
    )


def test_extract_exit_zero_unparseable_falls_back_to_renovate_hard_mode(
    tmp_path: pathlib.Path,
) -> None:
    """Same, but a hard-mode (default) renovate.json falls back to "false"."""
    _bootstrap(tmp_path)  # writes a hard-mode (default) renovate.json
    assert _extract_exit_zero("exit-zero: not-a-recognised-expression", tmp_path) == (
        "false"
    )


def test_extract_exit_zero_unparseable_defaults_false_without_renovate_json(
    tmp_path: pathlib.Path,
) -> None:
    """No renovate.json at all -- nothing to fall back to, defaults to hard-gate."""
    assert _extract_exit_zero("exit-zero: not-a-recognised-expression", tmp_path) == (
        "false"
    )


def test_extract_exit_zero_matches_takes_priority_over_renovate() -> None:
    """A parseable line wins outright -- the renovate.json fallback is only
    consulted when the line itself doesn't match."""
    text = (
        "exit-zero: ${{ github.event_name == 'schedule' || "
        "github.event_name == 'workflow_dispatch' || true }}"
    )
    # No root/renovate.json is ever touched in this case; a bogus path proves
    # the match path doesn't need it.
    assert _extract_exit_zero(text, pathlib.Path("/nonexistent")) == "true"
