"""Tests for the bootstrap command and its helpers.

Covers _bootstrap_file and _ensure_gitignore_entry in isolation, and the full
_cmd_bootstrap dispatch (SKILL.md + CI workflow shims + .gitignore) via the
CLI entrypoint so the tests exercise the same code path a caller would use.
"""

from __future__ import annotations

import pathlib

import pytest
from conformance.bootstrap.render import MANAGED_WORKFLOWS, render
from conformance.cli import (
    _bootstrap_file,
    _cmd_bootstrap,
    _ensure_gitignore_entry,
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
# _ensure_gitignore_entry
# ---------------------------------------------------------------------------


def test_gitignore_appends_entry_to_existing_file(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("# existing\n*.pyc\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert "remediation/" in gi.read_text()


def test_gitignore_creates_file_if_absent(tmp_path: pathlib.Path) -> None:
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert (tmp_path / ".gitignore").read_text().strip() == "remediation/"


def test_gitignore_does_not_duplicate_existing_entry(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("remediation/\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert gi.read_text().count("remediation/") == 1


def test_gitignore_does_not_clobber_existing_content(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    original = "# my rules\n*.log\n.env\n"
    gi.write_text(original)
    _ensure_gitignore_entry(tmp_path, "remediation/")
    result = gi.read_text()
    assert result.startswith(original)
    assert "remediation/" in result


def test_gitignore_no_op_prints_ok(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".gitignore").write_text("remediation/\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert "ok" in capsys.readouterr().out


def test_gitignore_append_prints_appended(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert "appended" in capsys.readouterr().out


def test_gitignore_entry_match_is_exact_line(tmp_path: pathlib.Path) -> None:
    """'remediation/' must not be considered present just because 'remediation/runs/' exists."""
    gi = tmp_path / ".gitignore"
    gi.write_text("remediation/runs/\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert gi.read_text().count("remediation/") == 2


# ---------------------------------------------------------------------------
# _parse_bootstrap_args
# ---------------------------------------------------------------------------


def test_parse_bootstrap_args_defaults() -> None:
    result = _parse_bootstrap_args([])
    assert result == {
        "package_name": "app",
        "unit_tests_workflow": "tests.yaml",
        "app_name": "app",
        "app_image_name": "",
        "enable_e2e": "true",
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


def test_cmd_bootstrap_preserves_existing_gitignore_content(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    gi = tmp_path / ".gitignore"
    gi.write_text("*.pyc\n.env\n")
    _cmd_bootstrap([])
    content = gi.read_text()
    assert "*.pyc" in content
    assert ".env" in content
    assert "remediation/" in content


def test_cmd_bootstrap_returns_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _cmd_bootstrap([]) == 0


# ---------------------------------------------------------------------------
# Template fidelity assertions
# ---------------------------------------------------------------------------


def test_conformance_workflow_contains_event_name() -> None:
    """The bundled workflow uses event_name, not the stale sdk-ref input."""
    content = render("conformance.yaml")
    assert "event_name:" in content
    assert "sdk-ref" not in content


def test_all_shims_have_atlanhq_uses_reference() -> None:
    """Every managed workflow delegates to atlanhq/* (or a known inline file)."""
    # These files contain inline logic (no `uses: atlanhq/...`) but are still standard.
    inline_ok = {"release-gate.yaml"}
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
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.unlink()
    _cmd_bootstrap([])
    assert wf.exists()
    assert wf.read_text() == render("tests.yaml")


# ---------------------------------------------------------------------------
# tests.yaml template fidelity
# ---------------------------------------------------------------------------


def test_tests_yaml_workflow_name_is_capitalised() -> None:
    content = render("tests.yaml")
    assert "name: Tests\n" in content


def test_tests_yaml_contains_reusable_reference() -> None:
    content = render("tests.yaml")
    assert (
        "atlanhq/application-sdk/.github/workflows/tests-reusable.yaml@main" in content
    )


def test_tests_yaml_contains_tests_gate_job() -> None:
    content = render("tests.yaml")
    assert "tests-gate:" in content
    assert "name: Tests Gate" in content


def test_tests_yaml_gate_reads_reusable_outputs() -> None:
    content = render("tests.yaml")
    assert "needs.tests.outputs.tests-result" in content
    assert "needs.tests.outputs.e2e-result" in content


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
    assert "enable-e2e: true" in content


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
