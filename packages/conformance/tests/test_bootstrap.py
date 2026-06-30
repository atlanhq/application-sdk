"""Tests for the bootstrap command and its helpers.

Covers _bootstrap_file in isolation, and the full _cmd_bootstrap dispatch
(SKILL.md + CI workflow shims + .gitignore) via the CLI entrypoint so the
tests exercise the same code path a caller would use.
"""

from __future__ import annotations

import pathlib

import pytest
from conformance.bootstrap.render import MANAGED_WORKFLOWS, render
from conformance.cli import (
    _bootstrap_file,
    _cmd_bootstrap,
    _derive_app_name_from_dir,
    _parse_bootstrap_args,
)

# ---------------------------------------------------------------------------
# _bootstrap_file (always-overwrite semantics)
# ---------------------------------------------------------------------------


def test_bootstrap_file_creates_new(tmp_path: pathlib.Path) -> None:
    dest = tmp_path / "sub" / "SKILL.md"
    _bootstrap_file(dest, "content")
    assert dest.read_text() == "content"


def test_bootstrap_file_creates_parent_dirs(tmp_path: pathlib.Path) -> None:
    dest = tmp_path / "a" / "b" / "c" / "file.md"
    _bootstrap_file(dest, "content")
    assert dest.exists()


def test_bootstrap_file_overwrites_existing(tmp_path: pathlib.Path) -> None:
    dest = tmp_path / "SKILL.md"
    dest.write_text("original")
    _bootstrap_file(dest, "new content")
    assert dest.read_text() == "new content"


def test_bootstrap_file_prints_installed_for_new(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "SKILL.md"
    _bootstrap_file(dest, "content")
    assert "installed" in capsys.readouterr().out


def test_bootstrap_file_prints_updated_for_overwrite(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "SKILL.md"
    dest.write_text("old")
    _bootstrap_file(dest, "new")
    assert "updated" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _parse_bootstrap_args
# ---------------------------------------------------------------------------


def test_parse_bootstrap_args_defaults() -> None:
    result = _parse_bootstrap_args([])
    assert result == {
        "package_name": "app",
        "unit_tests_workflow": "tests.yaml",
        "app_name": "",
        "app_image_name": "",
        "enable_e2e": "true",
        "services_script": "",
        "enforce": "",
    }


def test_parse_bootstrap_args_space_separated() -> None:
    result = _parse_bootstrap_args(["--package-name", "myapp"])
    assert result["package_name"] == "myapp"


def test_parse_bootstrap_args_equals_form() -> None:
    result = _parse_bootstrap_args(["--unit-tests-workflow=custom.yaml"])
    assert result["unit_tests_workflow"] == "custom.yaml"


def test_parse_bootstrap_args_both_flags() -> None:
    result = _parse_bootstrap_args(
        ["--package-name", "connector", "--unit-tests-workflow", "ci.yaml"]
    )
    assert result["package_name"] == "connector"
    assert result["unit_tests_workflow"] == "ci.yaml"


def test_parse_bootstrap_args_app_name() -> None:
    result = _parse_bootstrap_args(["--app-name", "mysql"])
    assert result["app_name"] == "mysql"


def test_parse_bootstrap_args_app_name_equals_form() -> None:
    result = _parse_bootstrap_args(["--app-name=openapi"])
    assert result["app_name"] == "openapi"


def test_parse_bootstrap_args_app_image_name() -> None:
    result = _parse_bootstrap_args(["--app-image-name", "atlan-mysql-app"])
    assert result["app_image_name"] == "atlan-mysql-app"


def test_parse_bootstrap_args_enable_e2e_false() -> None:
    result = _parse_bootstrap_args(["--enable-e2e", "false"])
    assert result["enable_e2e"] == "false"


def test_parse_bootstrap_args_enable_e2e_equals_form() -> None:
    result = _parse_bootstrap_args(["--enable-e2e=false"])
    assert result["enable_e2e"] == "false"


# ---------------------------------------------------------------------------
# _cmd_bootstrap (full integration)
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_writes_skill_md(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    dest = tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md"
    assert dest.read_text() == render("remediate.md")


def test_cmd_bootstrap_writes_all_managed_workflows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf_dir = tmp_path / ".github" / "workflows"
    for name in MANAGED_WORKFLOWS:
        dest = wf_dir / name
        assert dest.exists(), f"Missing: {name}"
        assert dest.read_text() == render(name), f"Content mismatch: {name}"


def test_cmd_bootstrap_adds_remediation_to_gitignore(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert "remediation/" in (tmp_path / ".gitignore").read_text()


def test_cmd_bootstrap_always_overwrites_on_rerun(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running bootstrap always rewrites managed files (drift eradication)."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    # Corrupt one workflow, then verify bootstrap fixes it.
    wf = tmp_path / ".github" / "workflows" / "conformance.yaml"
    wf.write_text("corrupted content")
    _cmd_bootstrap([])
    assert wf.read_text() == render("conformance.yaml")


def test_cmd_bootstrap_gitignore_not_duplicated_on_rerun(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    _cmd_bootstrap([])
    assert (tmp_path / ".gitignore").read_text().count("remediation/") == 1


def test_cmd_bootstrap_does_not_modify_existing_gitignore(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap is write-if-absent for .gitignore; it never modifies an existing file."""
    monkeypatch.chdir(tmp_path)
    gi = tmp_path / ".gitignore"
    original = "*.pyc\n.env\n"
    gi.write_text(original)
    _cmd_bootstrap([])
    assert gi.read_text() == original


def test_cmd_bootstrap_returns_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _cmd_bootstrap([]) == 0


# ---------------------------------------------------------------------------
# Template fidelity assertions
# ---------------------------------------------------------------------------


def test_parse_bootstrap_args_enforce_false() -> None:
    result = _parse_bootstrap_args(["--enforce", "false"])
    assert result["enforce"] == "false"


def test_parse_bootstrap_args_enforce_true() -> None:
    result = _parse_bootstrap_args(["--enforce", "true"])
    assert result["enforce"] == "true"


def test_parse_bootstrap_args_enforce_equals_form() -> None:
    result = _parse_bootstrap_args(["--enforce=false"])
    assert result["enforce"] == "false"


def test_parse_bootstrap_args_enforce_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_bootstrap_args(["--enforce", "maybe"])
    assert exc_info.value.code == 2
    assert "--enforce" in capsys.readouterr().err


def test_conformance_yaml_default_exit_zero_false() -> None:
    """Default bootstrap renders exit-zero: false (hard gate)."""
    content = render("conformance.yaml")
    assert "exit-zero: false" in content


def test_conformance_yaml_exit_zero_true() -> None:
    """render() with exit_zero='true' renders exit-zero: true for soft mode."""
    content = render("conformance.yaml", exit_zero="true")
    assert "exit-zero: true" in content


def test_cmd_bootstrap_enforce_false_writes_soft_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--enforce false writes exit-zero: true into conformance.yaml."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    conformance = (tmp_path / ".github" / "workflows" / "conformance.yaml").read_text()
    assert "exit-zero: true" in conformance


def test_cmd_bootstrap_enforce_true_writes_hard_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--enforce true writes exit-zero: false into conformance.yaml."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "true"])
    conformance = (tmp_path / ".github" / "workflows" / "conformance.yaml").read_text()
    assert "exit-zero: false" in conformance


def test_cmd_bootstrap_no_enforce_defaults_hard_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --enforce, bootstrap defaults to hard mode (exit-zero: false)."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    conformance = (tmp_path / ".github" / "workflows" / "conformance.yaml").read_text()
    assert "exit-zero: false" in conformance


def test_conformance_workflow_contains_event_name() -> None:
    """The bundled workflow uses event_name, not the stale sdk-ref input."""
    content = render("conformance.yaml")
    assert "event_name:" in content
    assert "sdk-ref" not in content


def test_conformance_upload_sarif_workflow_run_trigger() -> None:
    """Upload workflow is triggered by the Conformance workflow_run on main."""
    content = render("conformance-upload-sarif.yaml")
    assert 'workflows: ["Conformance"]' in content
    assert "workflow_run" in content
    assert "branches: [main]" in content


def test_conformance_upload_sarif_decoupled_from_gate() -> None:
    """Upload workflow uses continue-on-error on download so it always exits 0."""
    content = render("conformance-upload-sarif.yaml")
    assert "continue-on-error: true" in content


def test_conformance_upload_sarif_passes_ref_and_sha() -> None:
    """Upload workflow passes head_branch and head_sha so SARIF is anchored to the triggering commit."""
    content = render("conformance-upload-sarif.yaml")
    assert "workflow_run.head_branch" in content
    assert "workflow_run.head_sha" in content


def test_conformance_upload_sarif_has_required_permissions() -> None:
    """security-events: write uploads SARIF; actions: read downloads artifacts from the triggering run."""
    content = render("conformance-upload-sarif.yaml")
    assert "security-events: write" in content
    assert "actions: read" in content


def test_conformance_upload_sarif_covers_all_series() -> None:
    """Upload workflow covers all four conformance series slugs."""
    content = render("conformance-upload-sarif.yaml")
    for slug in ("ci", "error-handling", "prescriptions", "optimizations"):
        assert slug in content, f"Missing series slug: {slug}"


def test_all_shims_have_atlanhq_uses_reference() -> None:
    """Every managed workflow delegates to atlanhq/* (or a known inline file)."""
    # These files contain inline logic (no `uses: atlanhq/...`) but are still standard.
    inline_ok = {"release-gate.yaml", "conformance-upload-sarif.yaml"}
    for name in MANAGED_WORKFLOWS:
        content = render(name)
        if name in inline_ok:
            continue
        assert "atlanhq/" in content, f"Missing atlanhq/ uses: reference in {name}"


def test_no_jinja2_placeholders_in_rendered_output() -> None:
    """Rendered templates must not contain any un-substituted << >> tokens."""
    for name in MANAGED_WORKFLOWS:
        content = render(name)
        assert "<< " not in content, f"Unresolved jinja2 placeholder in {name}"
        assert " >>" not in content, f"Unresolved jinja2 placeholder in {name}"


# ---------------------------------------------------------------------------
# Templating: parameterised substitution
# ---------------------------------------------------------------------------


def test_docstring_coverage_default_package_name() -> None:
    content = render("docstring-coverage.yaml")
    assert 'package_name: "app"' in content


def test_docstring_coverage_custom_package_name() -> None:
    content = render("docstring-coverage.yaml", package_name="myconnector")
    assert 'package_name: "myconnector"' in content
    assert "myconnector" in content


def test_build_and_publish_default_unit_tests_workflow() -> None:
    content = render("build-and-publish.yaml")
    assert 'unit_tests_workflow_file: "tests.yaml"' in content


def test_build_and_publish_custom_unit_tests_workflow() -> None:
    content = render("build-and-publish.yaml", unit_tests_workflow="ci-tests.yaml")
    assert 'unit_tests_workflow_file: "ci-tests.yaml"' in content


def test_cmd_bootstrap_custom_args_propagate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(
        ["--package-name", "myapp", "--unit-tests-workflow", "run-tests.yaml"]
    )
    docstring = (
        tmp_path / ".github" / "workflows" / "docstring-coverage.yaml"
    ).read_text()
    build = (tmp_path / ".github" / "workflows" / "build-and-publish.yaml").read_text()
    assert 'package_name: "myapp"' in docstring
    assert 'unit_tests_workflow_file: "run-tests.yaml"' in build


# ---------------------------------------------------------------------------
# renovate.json scaffold — write-if-absent semantics
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_writes_renovate_json(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert (tmp_path / "renovate.json").exists()


def test_cmd_bootstrap_renovate_json_not_in_managed_workflows() -> None:
    assert "renovate.json" not in MANAGED_WORKFLOWS


def test_cmd_bootstrap_renovate_json_is_write_if_absent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second bootstrap run must NOT overwrite an already-customised renovate.json."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    rj = tmp_path / "renovate.json"
    rj.write_text('{"customised": true}\n')
    _cmd_bootstrap([])
    assert rj.read_text() == '{"customised": true}\n'


def test_cmd_bootstrap_renovate_json_recreated_when_deleted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete renovate.json → re-run bootstrap → file regenerated from canonical."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    rj = tmp_path / "renovate.json"
    rj.unlink()
    _cmd_bootstrap([])
    assert rj.exists()
    assert rj.read_text() == render("renovate.json")


def test_renovate_json_contains_fleet_preset() -> None:
    content = render("renovate.json")
    assert "atlanhq/application-sdk//renovate-config/default.json" in content


def test_renovate_json_contains_schema() -> None:
    content = render("renovate.json")
    assert "renovate-schema.json" in content


def test_renovate_json_default_no_automerge_override() -> None:
    """Default bootstrap does not add automerge overrides (auto-merge enabled via preset)."""
    content = render("renovate.json")
    assert '"automerge": false' not in content
    assert "packageRules" not in content


def test_renovate_json_automerge_false_adds_overrides() -> None:
    """--automerge false injects catch-all rule that disables auto-merge."""
    content = render("renovate.json", automerge="false")
    assert '"automerge": false' in content
    assert '"platformAutomerge": false' in content
    assert "packageRules" in content
    assert "lockFileMaintenance" in content


def test_renovate_json_uses_match_package_names_not_patterns() -> None:
    """Renovate v37+ deprecates matchPackagePatterns; template must use matchPackageNames."""
    content = render("renovate.json", automerge="false")
    assert "matchPackageNames" in content
    assert "matchPackagePatterns" not in content


def test_renovate_json_automerge_false_is_valid_json() -> None:
    """Rendered renovate.json with automerge=false is parseable JSON."""
    import json

    content = render("renovate.json", automerge="false")
    parsed = json.loads(content)
    assert parsed["extends"] == [
        "github>atlanhq/application-sdk//renovate-config/default.json"
    ]
    assert parsed["lockFileMaintenance"]["automerge"] is False
    assert any(r.get("automerge") is False for r in parsed.get("packageRules", []))


def test_cmd_bootstrap_enforce_false_writes_soft_renovate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--enforce false injects disable-automerge overrides into the renovate.json scaffold."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    renovate = (tmp_path / "renovate.json").read_text()
    assert '"automerge": false' in renovate
    assert "packageRules" in renovate


def test_cmd_bootstrap_no_enforce_hard_renovate_default(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default bootstrap (no --enforce) writes minimal renovate.json (auto-merge via preset)."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    renovate = (tmp_path / "renovate.json").read_text()
    assert '"automerge": false' not in renovate


def test_cmd_bootstrap_enforce_force_overwrites_existing_renovate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--enforce with custom content writes a .bak before overwriting."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    rj = tmp_path / "renovate.json"
    rj.write_text('{"customised": true}\n')
    # Re-run with --enforce false → must overwrite and back up custom content.
    _cmd_bootstrap(["--enforce", "false"])
    content = rj.read_text()
    assert '"automerge": false' in content  # soft-mode overrides applied
    assert (tmp_path / "renovate.json.bak").exists()  # custom content backed up


def test_cmd_bootstrap_enforce_idempotent_on_matching_content(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--enforce is a no-op (and prints 'up to date') when file already matches target."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    rj = tmp_path / "renovate.json"
    mtime_before = rj.stat().st_mtime
    capsys.readouterr()  # clear
    _cmd_bootstrap(["--enforce", "false"])
    assert "up to date" in capsys.readouterr().out
    # File must not have been rewritten (mtime unchanged).
    assert rj.stat().st_mtime == mtime_before


def test_cmd_bootstrap_enforce_no_bak_when_canonical_content(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching from soft to hard mode doesn't write .bak (canonical content)."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    rj = tmp_path / "renovate.json"
    # Upgrade to hard mode — existing content is the canonical soft render, not custom.
    _cmd_bootstrap(["--enforce", "true"])
    assert not (tmp_path / "renovate.json.bak").exists()


def test_cmd_bootstrap_enforce_true_force_overwrites_existing_renovate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--enforce true force-overwrites a soft-mode renovate.json to re-enable auto-merge."""
    monkeypatch.chdir(tmp_path)
    # First bootstrap in soft mode.
    _cmd_bootstrap(["--enforce", "false"])
    rj = tmp_path / "renovate.json"
    assert '"automerge": false' in rj.read_text()
    # Upgrade to hard mode.
    _cmd_bootstrap(["--enforce", "true"])
    content = rj.read_text()
    assert '"automerge": false' not in content  # overrides removed


# ---------------------------------------------------------------------------
# tests.yaml scaffold — write-if-absent semantics
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_writes_tests_yaml(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert (tmp_path / ".github" / "workflows" / "tests.yaml").exists()


def test_cmd_bootstrap_tests_yaml_not_in_managed_workflows() -> None:
    assert "tests.yaml" not in MANAGED_WORKFLOWS


def test_cmd_bootstrap_tests_yaml_is_write_if_absent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second bootstrap run must NOT overwrite an already-customised tests.yaml."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text("# my custom content\n")
    _cmd_bootstrap([])
    assert wf.read_text() == "# my custom content\n"


def test_cmd_bootstrap_tests_yaml_recreated_when_deleted(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete tests.yaml → re-run bootstrap → file regenerated from canonical."""
    app_dir = tmp_path / "atlan-myapp-app"
    app_dir.mkdir()
    monkeypatch.chdir(app_dir)
    _cmd_bootstrap([])
    wf = app_dir / ".github" / "workflows" / "tests.yaml"
    wf.unlink()
    _cmd_bootstrap([])
    assert wf.exists()
    assert wf.read_text() == render("tests.yaml", app_name="myapp")


# ---------------------------------------------------------------------------
# tests.yaml template fidelity
# ---------------------------------------------------------------------------


def test_tests_yaml_workflow_name_is_capitalized() -> None:
    content = render("tests.yaml")
    assert "name: Tests\n" in content


def test_tests_yaml_contains_reusable_reference() -> None:
    content = render("tests.yaml")
    assert (
        "atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main" in content
    )


def test_tests_yaml_contains_remediation_header() -> None:
    content = render("tests.yaml")
    assert "bootstrap" in content
    assert "C002" in content


def test_tests_yaml_contains_services_script_hint() -> None:
    content = render("tests.yaml")
    assert "# services-script:" in content


def test_tests_yaml_contains_secrets_inherit() -> None:
    content = render("tests.yaml")
    assert "secrets: inherit" in content


def test_tests_yaml_default_app_name() -> None:
    content = render("tests.yaml")
    assert 'app-name: "app"' in content
    assert 'app-image-name: "atlan-app-app"' in content
    assert "enable-e2e:" not in content


def test_tests_yaml_custom_app_name_and_image() -> None:
    content = render("tests.yaml", app_name="mysql", app_image_name="atlan-mysql-app")
    assert 'app-name: "mysql"' in content
    assert 'app-image-name: "atlan-mysql-app"' in content


def test_tests_yaml_app_image_derived_when_not_given() -> None:
    content = render("tests.yaml", app_name="openapi")
    assert 'app-image-name: "atlan-openapi-app"' in content


def test_tests_yaml_enable_e2e_false() -> None:
    content = render("tests.yaml", enable_e2e="false")
    assert "enable-e2e: false" in content


def test_tests_yaml_services_script_active() -> None:
    content = render("tests.yaml", services_script=".github/test/setup-services.sh")
    assert 'services-script: ".github/test/setup-services.sh"' in content
    # The commented hint must NOT appear when the value is active.
    assert "# services-script:" not in content


def test_tests_yaml_no_unresolved_placeholders() -> None:
    content = render("tests.yaml")
    assert "<< " not in content
    assert " >>" not in content


def test_cmd_bootstrap_custom_app_args_propagate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(
        [
            "--app-name",
            "mysql",
            "--app-image-name",
            "custom-image-name",
            "--enable-e2e",
            "false",
        ]
    )
    tests = (tmp_path / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'app-name: "mysql"' in tests
    assert 'app-image-name: "custom-image-name"' in tests
    assert "enable-e2e: false" in tests


# ---------------------------------------------------------------------------
# Auto-detection: app name from atlan.yaml
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_reads_app_name_from_atlan_yaml(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bootstrap reads `name:` from atlan.yaml when --app-name is not supplied."""
    (tmp_path / "atlan.yaml").write_text("name: openapi\ndisplay_name: OpenAPI\n")
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    tests = (tmp_path / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'app-name: "openapi"' in tests
    assert 'app-image-name: "atlan-openapi-app"' in tests


def test_cmd_bootstrap_strips_quotes_from_atlan_yaml_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quoted name: \"openapi\" in atlan.yaml should still resolve to openapi."""
    (tmp_path / "atlan.yaml").write_text('name: "openapi"\n')
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    tests = (tmp_path / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'app-name: "openapi"' in tests


def test_cmd_bootstrap_explicit_app_name_overrides_atlan_yaml(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit --app-name takes priority over atlan.yaml."""
    (tmp_path / "atlan.yaml").write_text("name: openapi\n")
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--app-name", "mysql"])
    tests = (tmp_path / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'app-name: "mysql"' in tests


def test_cmd_bootstrap_falls_back_to_dir_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without atlan.yaml, app name is derived from the repo directory name."""
    app_dir = tmp_path / "atlan-postgres-app"
    app_dir.mkdir()
    monkeypatch.chdir(app_dir)
    _cmd_bootstrap([])
    tests = (app_dir / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'app-name: "postgres"' in tests
    assert 'app-image-name: "atlan-postgres-app"' in tests


def test_cmd_bootstrap_atlan_yaml_takes_priority_over_dir_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """atlan.yaml name: takes priority over the directory name."""
    app_dir = tmp_path / "atlan-wrong-app"
    app_dir.mkdir()
    (app_dir / "atlan.yaml").write_text("name: openapi\n")
    monkeypatch.chdir(app_dir)
    _cmd_bootstrap([])
    tests = (app_dir / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'app-name: "openapi"' in tests


# ---------------------------------------------------------------------------
# Auto-detection: services-script from .github/test/setup-services.sh
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_detects_services_script(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bootstrap activates services-script when .github/test/setup-services.sh exists."""
    script = tmp_path / ".github" / "test" / "setup-services.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    tests = (tmp_path / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'services-script: ".github/test/setup-services.sh"' in tests
    assert "# services-script:" not in tests


def test_cmd_bootstrap_no_services_script_when_absent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without setup-services.sh the services-script line stays commented out."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    tests = (tmp_path / ".github" / "workflows" / "tests.yaml").read_text()
    assert "# services-script:" in tests
    assert 'services-script: ".github/test/setup-services.sh"' not in tests


def test_cmd_bootstrap_explicit_services_script_overrides_autodetect(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit --services-script takes priority over auto-detected path."""
    script = tmp_path / ".github" / "test" / "setup-services.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\n")
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--services-script", ".github/test/custom-setup.sh"])
    tests = (tmp_path / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'services-script: ".github/test/custom-setup.sh"' in tests


# ---------------------------------------------------------------------------
# enable-e2e: omitted when default (true)
# ---------------------------------------------------------------------------


def test_tests_yaml_enable_e2e_omitted_when_default() -> None:
    """enable-e2e: true is the reusable workflow default — don't emit it."""
    content = render("tests.yaml")
    assert "enable-e2e:" not in content


def test_tests_yaml_enable_e2e_present_when_false() -> None:
    """enable-e2e: false must appear explicitly to opt out of e2e."""
    content = render("tests.yaml", enable_e2e="false")
    assert "enable-e2e: false" in content


# ---------------------------------------------------------------------------
# _derive_app_name_from_dir unit tests
# ---------------------------------------------------------------------------


def test_derive_strips_atlan_prefix_and_app_suffix(tmp_path: pathlib.Path) -> None:
    assert _derive_app_name_from_dir(tmp_path / "atlan-openapi-app") == "openapi"


def test_derive_strips_only_prefix(tmp_path: pathlib.Path) -> None:
    assert _derive_app_name_from_dir(tmp_path / "atlan-openapi") == "openapi"


def test_derive_strips_only_suffix(tmp_path: pathlib.Path) -> None:
    assert _derive_app_name_from_dir(tmp_path / "my-connector-app") == "my-connector"


def test_derive_no_affixes(tmp_path: pathlib.Path) -> None:
    assert _derive_app_name_from_dir(tmp_path / "postgres") == "postgres"


def test_derive_hello_world(tmp_path: pathlib.Path) -> None:
    assert (
        _derive_app_name_from_dir(tmp_path / "atlan-hello-world-app") == "hello-world"
    )


def test_derive_falls_back_to_app_for_bare_atlan(tmp_path: pathlib.Path) -> None:
    # "atlan-app" → strip prefix → "app" → strip suffix ("app" doesn't end with "-app") → "app"
    assert _derive_app_name_from_dir(tmp_path / "atlan-app") == "app"
