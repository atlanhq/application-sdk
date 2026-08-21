"""Tests for C002 BootstrapWorkflowDrift check.

Covers:
- Clean (freshly bootstrapped) repo → no findings
- Missing managed workflow → one C002 WARN finding per absent file
- Byte-drifted managed workflow → one C002 WARN finding
- Parameterised files with a custom-but-valid value → no finding (structural match)
- Parameterised files with structural drift beyond the value → finding
- Retired managed workflow still on disk → finding; absent → no finding
"""

from __future__ import annotations

import pathlib
import re

import pytest
from conformance.bootstrap import extract as extract_mod
from conformance.bootstrap.render import (
    MANAGED_ACTION_FILES,
    MANAGED_WORKFLOWS,
    RETIRED_WORKFLOWS,
    render,
)
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
    # managed shims + retired shims + managed action files + tests.yaml and
    # renovate.json scaffolds.
    assert len(paths) == (
        len(MANAGED_WORKFLOWS) + len(RETIRED_WORKFLOWS) + len(MANAGED_ACTION_FILES) + 2
    )
    # None of them exist yet.
    assert all(not p.exists() for p in paths)


def test_discover_includes_retired_workflows(tmp_path: pathlib.Path) -> None:
    """A retired shim must still be discovered, or a repo that never re-runs
    bootstrap is never told the dead file is there."""
    names = {p.name for p in discover(tmp_path)}
    assert set(RETIRED_WORKFLOWS).issubset(names)


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


def test_build_and_publish_ghcr_base_opt_in_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """An app that self-selected the GHCR base redirect must not be flagged.

    Same reasoning as --system-deps below: the only "fix" for a C002 finding
    here is re-running bootstrap, which would send the app back to Harbor —
    and the SDK-side default won't flip for a long time, so opting in early has
    to be a first-class per-repo choice.
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "build-and-publish.yaml"
    wf.write_text(render("build-and-publish.yaml", use_ghcr_base="true"))
    assert scan_path(wf, tmp_path) == []


def test_build_and_publish_hand_added_ghcr_base_opt_in_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """The opt-in as an app would actually hand-write it into the `with:` block."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "build-and-publish.yaml"
    wf.write_text(
        render("build-and-publish.yaml").replace(
            "    secrets: inherit", "      use_ghcr_base: true\n    secrets: inherit"
        )
    )
    assert scan_path(wf, tmp_path) == []


def test_build_and_publish_ghcr_base_opt_in_with_structural_drift_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """Only the opt-in itself is per-repo — other edits are still drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "build-and-publish.yaml"
    wf.write_text(
        render("build-and-publish.yaml", use_ghcr_base="true").replace(
            "    secrets: inherit", "      channel: nightly\n    secrets: inherit"
        )
    )
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C002"


def test_checks_custom_system_deps_not_flagged(tmp_path: pathlib.Path) -> None:
    """A repo that used --system-deps must not be flagged: the only
    "fix" for a C002 finding here is re-running bootstrap, which would delete
    the build-header step its pre-commit job needs."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "checks.yml"
    wf.write_text(render("checks.yml", system_deps="libkrb5-dev gcc python3-dev"))
    assert scan_path(wf, tmp_path) == []


def test_checks_system_deps_structural_drift_flagged(tmp_path: pathlib.Path) -> None:
    """Only the package list is per-repo — other edits to the step are drift."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "checks.yml"
    canonical = render("checks.yml", system_deps="libkrb5-dev")
    wf.write_text(
        canonical.replace("    timeout-minutes: 10", "    timeout-minutes: 5")
    )
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert "drifted" in findings[0].message


def test_checks_hand_written_system_deps_step_flagged_then_fixed_by_bootstrap(
    tmp_path: pathlib.Path,
) -> None:
    """A pre-flag hand-added step is drift (its shape isn't canonical), and the
    prescribed fix — re-run bootstrap — now preserves the packages instead of
    deleting them, so the finding clears without breaking the job."""
    _bootstrap(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "checks.yml"
    wf.write_text(
        wf.read_text().replace(
            "      - uses: atlanhq/application-sdk",
            "      - name: Install system dependencies for pykerberos\n"
            "        run: |\n"
            "          sudo apt-get update\n"
            "          sudo apt-get install -y libkrb5-dev gcc python3-dev\n"
            "      - uses: atlanhq/application-sdk",
        )
    )
    assert len(scan_path(wf, tmp_path)) == 1
    _bootstrap(tmp_path)
    assert scan_path(wf, tmp_path) == []
    assert "libkrb5-dev gcc python3-dev" in wf.read_text()


# ---------------------------------------------------------------------------
# Parameterised files: structural drift beyond the value → finding
# ---------------------------------------------------------------------------


def test_build_and_publish_structural_drift_flagged(tmp_path: pathlib.Path) -> None:
    """Structural change (not just the value) in a templated file is still flagged."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "build-and-publish.yaml"
    # Write canonical then inject an extra job-level key.
    canonical = render("build-and-publish.yaml")
    drifted = canonical.replace("jobs:\n", "jobs:\n  injected:\n    runs-on: ubuntu\n")
    assert drifted != canonical
    wf.write_text(drifted)
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert "drifted" in findings[0].message


# ---------------------------------------------------------------------------
# Retired shims (FND-381): present on disk → finding; gone → silent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", RETIRED_WORKFLOWS)
def test_retired_workflow_still_present_is_flagged(
    tmp_path: pathlib.Path, name: str
) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / name
    wf.write_text("name: legacy\n")
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    assert "retired" in findings[0].message
    assert findings[0].file == f".github/workflows/{name}"


@pytest.mark.parametrize("name", RETIRED_WORKFLOWS)
def test_retired_workflow_absent_is_not_flagged(
    tmp_path: pathlib.Path, name: str
) -> None:
    """The inverse of a managed shim: absence is the desired end state, not a
    finding — otherwise every repo that already converged reports drift."""
    wf = tmp_path / ".github" / "workflows" / name
    assert scan_path(wf, tmp_path) == []


def test_bootstrapped_repo_has_no_retired_workflow(tmp_path: pathlib.Path) -> None:
    """End to end: bootstrap removes the file, so the sweep is clean after."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name in RETIRED_WORKFLOWS:
        (wf_dir / name).write_text("name: legacy\n")
    _bootstrap(tmp_path)
    findings = []
    for path in discover(tmp_path):
        findings.extend(scan_path(path, tmp_path))
    assert findings == [], [f.message for f in findings]


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


def test_tests_yaml_raised_coverage_floor_not_flagged(tmp_path: pathlib.Path) -> None:
    """An app raising its own unit-coverage floor is a recognised param.

    The whole point of the allowance: a connector that opted UP must not report
    as drifted, since "fixing" the finding would undo the stricter bar.
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(render("tests.yaml", app_name="mysql", unit_coverage_fail_under="40"))
    findings = scan_path(wf, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_tests_yaml_hand_added_coverage_floor_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """The same allowance for a line added by hand rather than by bootstrap.

    Pinned against the exact shape the fleet already carries — a bare
    ``unit-coverage-fail-under: "90"`` as the first entry of the ``with:``
    block, which is what the apps that raised their floor actually wrote. The
    template renders the line in that position, and bare rather than with a
    surrounding comment, precisely so those files match the canonical instead
    of reporting drift for having opted up.
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(
        render("tests.yaml", app_name="mysql").replace(
            '      app-name: "mysql"\n',
            '      unit-coverage-fail-under: "90"\n      app-name: "mysql"\n',
        )
    )
    findings = scan_path(wf, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_tests_yaml_sub_floor_coverage_flagged_and_explained(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A floor BELOW the SDK's stays drift, and the message says why.

    Without the explanation the finding reads as unspecified "structural
    drift", and the remediation (--resync) silently deletes the app's own line
    — so the message has to name the value, the SDK floor, and that outcome.
    """
    monkeypatch.setattr(extract_mod, "SDK_UNIT_COVERAGE_FLOOR", 40)
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(render("tests.yaml", app_name="mysql", unit_coverage_fail_under="20"))
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    msg = findings[0].message
    assert "unit-coverage-fail-under: 20" in msg
    assert "40" in msg
    assert "--resync" in msg


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


def test_tests_yaml_drift_message_names_the_resync_flag(
    tmp_path: pathlib.Path,
) -> None:
    """Drift message points at the flag that actually fixes it.

    The remediation used to be "delete the file and re-run", which discards
    every per-repo customisation to land a structural catch-up.
    ``--resync`` preserves them, so the message must name it — a
    finding whose stated fix is more destructive than necessary is one people
    reasonably refuse to act on.
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text("structural drift")
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    msg = findings[0].message
    assert "--resync" in msg
    assert "bootstrap" in msg
    assert "delete" not in msg.lower()


def test_tests_yaml_force_external_runtime_not_flagged(tmp_path: pathlib.Path) -> None:
    """A forced external runtime is a recognised param, not drift (FND-604).

    It has to be both: preserved by --resync *and* invisible to this checker.
    Flagging it would tell an app whose main.py genuinely needs external daprd
    that its own working config is non-conformant.
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(render("tests.yaml", app_name="mysql", force_external_runtime="true"))
    findings = scan_path(wf, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_tests_yaml_explicit_secrets_mapping_not_flagged(
    tmp_path: pathlib.Path,
) -> None:
    """An explicit ``secrets:`` mapping is a recognised param, not drift.

    It is the norm across the migrated fleet, not an oddity: ``secrets: inherit``
    can neither compose nor rename, so any connector with a real source system
    has to map its credentials by name.
    """
    block = (
        "    secrets:\n"
        "      E2E_SOURCE_ENV_JSON: |\n"
        '        {"E2E_WIDGET_HOST": ${{ toJSON(secrets.E2E_WIDGET_HOST) }}}'
    )
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(render("tests.yaml", app_name="mysql", secrets_block=block))
    findings = scan_path(wf, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_tests_yaml_unpreservable_declaration_explained_in_the_finding(
    tmp_path: pathlib.Path,
) -> None:
    """When --resync will refuse, the finding that recommends it must say so.

    Otherwise the message promises a re-render that then does not happen, and
    the app owner has to run the command to find out why — the same
    invisibility that let FND-604 recur.
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    wf = wf_dir / "tests.yaml"
    wf.write_text(
        render("tests.yaml", app_name="mysql")
        + "\n  tests-passed:\n    runs-on: ubuntu-latest\n"
    )
    findings = scan_path(wf, tmp_path)
    assert len(findings) == 1
    msg = findings[0].message
    assert "tests-passed" in msg
    assert "REFUSES" in msg


def test_tests_yaml_absent_message_does_not_name_the_resync_flag(
    tmp_path: pathlib.Path,
) -> None:
    """An absent tests.yaml is scaffolded by a bare re-run — no flag needed.

    Separate message from the drift case: there is nothing on disk to
    preserve, so telling the user to pass a preservation flag would imply the
    bare re-run is unsafe when it is exactly the right command.
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    findings = scan_path(wf_dir / "tests.yaml", tmp_path)
    assert len(findings) == 1
    msg = findings[0].message
    assert "absent" in msg.lower()
    assert "--resync" not in msg


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


def test_renovate_json_drift_message_names_the_resync_flag(
    tmp_path: pathlib.Path,
) -> None:
    """Drift message points at the flag that actually fixes it.

    Mirrors the tests.yaml case: the old advice was "delete and re-run", which
    discards the file wholesale. It must also keep --resync distinct from the
    mode flags — those CHANGE the auto-merge policy, which is not what a
    structural catch-up should do.
    """
    rj = tmp_path / "renovate.json"
    rj.write_text("{}\n")
    findings = scan_path(rj, tmp_path)
    assert len(findings) == 1
    msg = findings[0].message
    assert "--resync" in msg
    assert "bootstrap" in msg
    assert "delete" not in msg.lower()


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
