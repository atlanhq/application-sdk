"""Tests for check_e2e_scaffold.py — the full-DAG scaffold precondition (FND-656 B2).

The load-bearing property is the one that could red the fleet: the check must
pass for every repo whose e2e legs work today, i.e. it must require exactly what
``tests-reusable.yaml`` pins and nothing more.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_SCRIPT = Path(__file__).resolve().parents[1] / "check_e2e_scaffold.py"
_spec = importlib.util.spec_from_file_location("check_e2e_scaffold", _SCRIPT)
assert _spec and _spec.loader
scaffold = importlib.util.module_from_spec(_spec)
sys.modules["check_e2e_scaffold"] = scaffold
_spec.loader.exec_module(scaffold)


def _full_dag_repo(root: Path) -> Path:
    """A repo carrying exactly what tests-reusable.yaml's e2e job pins."""
    (root / ".github/e2e").mkdir(parents=True)
    (root / ".github/e2e/app.yaml").write_text("name: x\n")
    (root / ".github/e2e/make-secrets-e2e-full.py").write_text("# writes creds\n")
    return root


# ── The no-regression case ───────────────────────────────────────────────────


def test_onboarded_repo_has_no_gaps(tmp_path: Path) -> None:
    assert scaffold.find_gaps(root=_full_dag_repo(tmp_path)) == []


def test_repo_root_app_yaml_is_accepted(tmp_path: Path) -> None:
    """The sdr-e2e action falls back to a repo-root app.yaml, so this check must
    too — requiring it inside the config dir would red the adopter layout."""
    (tmp_path / ".github/e2e").mkdir(parents=True)
    (tmp_path / ".github/e2e/make-secrets-e2e-full.py").write_text("#\n")
    (tmp_path / "app.yaml").write_text("name: x\n")

    assert scaffold.find_gaps(root=tmp_path) == []


def test_extra_optional_pieces_are_not_required(tmp_path: Path) -> None:
    """components-dir and compose-overlay are skipped/defaulted when absent, so
    demanding them here would fail runs that pass today."""
    root = _full_dag_repo(tmp_path)
    assert not (root / ".github/e2e/e2e-full-components").exists()
    assert not (root / ".github/e2e/e2e-full-docker-compose.yaml").exists()

    assert scaffold.find_gaps(root=root) == []


def test_exit_code_zero_on_an_onboarded_repo(tmp_path: Path) -> None:
    assert scaffold.main(["--root", str(_full_dag_repo(tmp_path))]) == 0


# ── The gaps ────────────────────────────────────────────────────────────────


def test_repo_with_neither_config_dir_reports_every_gap(tmp_path: Path) -> None:
    """quicksight / cosmosdb: no e2e scaffold at all. One run must name all of
    it, not one file per 20-minute round trip."""
    gaps = scaffold.find_gaps(root=tmp_path)

    assert [gap.path for gap in gaps] == [
        ".github/e2e",
        ".github/e2e/app.yaml",
        ".github/e2e/make-secrets-e2e-full.py",
    ]


def test_sdr_only_repo_is_told_the_two_tiers_are_separate(tmp_path: Path) -> None:
    """dbt / teradata carry .github/sdr-e2e/ and no .github/e2e/. "You have the
    SDR scaffold, not the full-DAG one" is a different fix from "you have
    neither", and the path alone cannot tell them apart."""
    (tmp_path / ".github/sdr-e2e").mkdir(parents=True)

    remedy = next(
        gap.remedy
        for gap in scaffold.find_gaps(root=tmp_path)
        if gap.path == ".github/e2e"
    )

    assert ".github/sdr-e2e" in remedy
    assert "separate stacks" in remedy


def test_neither_dir_remedy_does_not_mention_the_sdr_dir(tmp_path: Path) -> None:
    """The inverse of the case above: naming a directory the repo does not have
    sends the reader looking for something that is not there."""
    remedy = next(
        gap.remedy
        for gap in scaffold.find_gaps(root=tmp_path)
        if gap.path == ".github/e2e"
    )

    assert ".github/sdr-e2e" not in remedy


def test_config_dir_present_but_secrets_script_missing(tmp_path: Path) -> None:
    """The dir alone is not the scaffold: the action hard-fails on the secrets
    script, so it has to be checked independently of the dir."""
    (tmp_path / ".github/e2e").mkdir(parents=True)
    (tmp_path / ".github/e2e/app.yaml").write_text("name: x\n")

    assert [gap.path for gap in scaffold.find_gaps(root=tmp_path)] == [
        ".github/e2e/make-secrets-e2e-full.py"
    ]


def test_config_dir_present_but_no_app_yaml_anywhere(tmp_path: Path) -> None:
    (tmp_path / ".github/e2e").mkdir(parents=True)
    (tmp_path / ".github/e2e/make-secrets-e2e-full.py").write_text("#\n")

    assert [gap.path for gap in scaffold.find_gaps(root=tmp_path)] == [
        ".github/e2e/app.yaml"
    ]


def test_a_directory_named_app_yaml_is_not_an_app_yaml(tmp_path: Path) -> None:
    """is_file(), not exists() — a directory of that name would pass an
    exists() check and then break envsubst inside the leg."""
    (tmp_path / ".github/e2e/app.yaml").mkdir(parents=True)
    (tmp_path / ".github/e2e/make-secrets-e2e-full.py").write_text("#\n")

    assert [gap.path for gap in scaffold.find_gaps(root=tmp_path)] == [
        ".github/e2e/app.yaml"
    ]


# ── The reported failure ────────────────────────────────────────────────────


def test_exit_code_one_and_one_error_annotation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert scaffold.main(["--root", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    # One annotation, not one per gap: GitHub surfaces the first and the operator
    # would fix a third of the problem per run.
    assert err.count("::error::") == 1
    assert "MISSING .github/e2e " in err
    assert "MISSING .github/e2e/make-secrets-e2e-full.py" in err


def test_error_says_where_it_would_otherwise_have_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point is the position of the failure, so the message has to say
    what the early exit saved — otherwise the next reader "helpfully" moves the
    check back down into the leg."""
    scaffold.main(["--root", str(tmp_path)])

    err = capsys.readouterr().err
    assert "tenant install" in err
    assert "discovery" in err


def test_error_offers_the_opt_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A connector that is not on the full-DAG tier yet needs a way to be green
    that is not "onboard right now"."""
    scaffold.main(["--root", str(tmp_path)])

    assert "enable-e2e: false" in capsys.readouterr().err


# ── The paths are a COPY of the caller's pins — keep them pinned ─────────────
# The whole value of this check is that it predicts what the e2e job will do, and
# it does that by duplicating two strings the job passes to the sdr-e2e action.
# Nothing in Python links the two files, so the failure mode is silent and
# expensive: someone repoints `config-dir` or `secrets-script`, and every repo
# gets a discovery-time check demanding a file no longer used while the real
# failure moves back into the leg. These read the workflow and compare.


def _e2e_action_inputs() -> dict:  # type: ignore[type-arg]
    """The `with:` block tests-reusable.yaml hands the sdr-e2e action."""
    reusable = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / ".github/workflows/tests-reusable.yaml"
        ).read_text(encoding="utf-8")
    )
    for step in reusable["jobs"]["e2e"]["steps"]:
        if "sdr-e2e" in str(step.get("uses", "")):
            return step["with"]
    raise AssertionError("the e2e job no longer invokes the sdr-e2e action")


def test_the_checked_config_dir_is_the_one_the_caller_pins() -> None:
    assert _e2e_action_inputs()["config-dir"] == scaffold.FULL_DAG_CONFIG_DIR, (
        "tests-reusable.yaml's e2e job pins a different config-dir than this "
        "check requires, so the check now demands a directory nothing reads and "
        "the real resolution failure is back inside the leg"
    )


def test_the_checked_secrets_script_is_the_one_the_caller_pins() -> None:
    assert _e2e_action_inputs()["secrets-script"] == scaffold.FULL_DAG_SECRETS_SCRIPT, (
        "tests-reusable.yaml's e2e job pins a different secrets-script than this "
        "check requires — the check would pass repos the action then fails, and "
        "fail repos the action would have accepted"
    )


def test_the_scaffold_check_runs_in_discover_e2e() -> None:
    """Position is the point: in the leg it is worth nothing, because that is
    where the failure already happens."""
    reusable = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / ".github/workflows/tests-reusable.yaml"
        ).read_text(encoding="utf-8")
    )
    runs = [
        str(step.get("run", "")) for step in reusable["jobs"]["discover-e2e"]["steps"]
    ]

    assert any("check_e2e_scaffold.py" in run for run in runs), (
        "the scaffold precondition must run in discover-e2e — the first job on "
        "the e2e path, before two image builds, a tenant lease and a tenant "
        "install have been spent"
    )
