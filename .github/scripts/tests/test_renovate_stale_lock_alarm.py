"""Tests for the stale lock-maintenance PR alarm (FND-5471)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import renovate_stale_lock_alarm as alarm
from _workflows import job, load_workflow, step_named

try:  # The category pin's other half — see the last test.
    from conformance.renovate.models import Category as _Category
except ImportError:  # pragma: no cover - depends on the runner's environment
    _Category = None

HERE = "atlanhq/application-sdk"


def _write_repo_report(out_dir: Path, repo: str, prs: list[dict[str, object]]) -> None:
    """Write one repos/<slug>.json in the shape the renovate-scan CLI emits."""
    repos = out_dir / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    (repos / f"{repo.split('/')[1]}.json").write_text(
        json.dumps({"repo": repo, "openPRs": prs}), encoding="utf-8"
    )


def _pr(
    number: int, age: int, category: str = alarm.LOCK_MAINTENANCE
) -> dict[str, object]:
    return {
        "number": number,
        "url": f"https://github.com/atlanhq/example/pull/{number}",
        "category": category,
        "blockingReason": "approval_missing",
        "ageDays": age,
    }


def _run(out_dir: Path) -> int:
    return alarm.main(["--out-dir", str(out_dir), "--fail-repo", HERE])


def test_a_fresh_lock_pr_here_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_repo_report(tmp_path, HERE, [_pr(1, alarm.MAX_AGE_DAYS - 1)])

    assert _run(tmp_path) == 0
    assert "stale lock-maintenance PRs here (fatal): 0" in capsys.readouterr().out


def test_a_week_old_lock_pr_here_fails_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The FND-5471 state: the PR sat open with auto-merge armed and nothing red."""
    _write_repo_report(tmp_path, HERE, [_pr(4718, alarm.MAX_AGE_DAYS)])

    assert _run(tmp_path) == 1
    captured = capsys.readouterr()
    assert f"{HERE}#4718  age=7d" in captured.out
    assert "lock refresh lane is stuck" in captured.err


def test_the_repo_match_ignores_case(tmp_path: Path) -> None:
    _write_repo_report(tmp_path, "AtlanHQ/Application-SDK", [_pr(1, 30)])

    assert _run(tmp_path) == 1


def test_only_lock_maintenance_counts(tmp_path: Path) -> None:
    # Other lanes have their own owners and their own reasons to wait (a pkl
    # bump is reviewed by design); this alarm is about the lock refresh alone.
    _write_repo_report(tmp_path, HERE, [_pr(2, 30, category="python-dep")])

    assert _run(tmp_path) == 0


def test_a_stale_lock_pr_elsewhere_warns_but_never_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An hourly job red on PRs other repos own would be red forever."""
    _write_repo_report(tmp_path, HERE, [])
    _write_repo_report(tmp_path, "atlanhq/atlan-example-app", [_pr(9, 40)])

    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "elsewhere (not alarmed): 1" in out
    assert "atlanhq/atlan-example-app#9" in out
    assert "::warning::1 lock-maintenance PR(s)" in out


def test_no_report_for_this_repo_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scan that dropped the repo must not read as a healthy lane."""
    _write_repo_report(tmp_path, "atlanhq/atlan-example-app", [])

    assert _run(tmp_path) == 1
    assert "no report for atlanhq/application-sdk" in capsys.readouterr().err


def test_an_empty_scan_fails_closed(tmp_path: Path) -> None:
    assert _run(tmp_path) == 1


def test_an_unreadable_report_is_skipped_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_repo_report(tmp_path, HERE, [])
    (tmp_path / "repos" / "broken.json").write_text("{", encoding="utf-8")

    assert _run(tmp_path) == 0
    assert "unreadable repo report: broken.json" in capsys.readouterr().err


@pytest.mark.parametrize("age", [None, "9"])
def test_a_pr_with_no_usable_age_is_not_judged(tmp_path: Path, age: object) -> None:
    pr = _pr(3, 0)
    pr["ageDays"] = age
    _write_repo_report(tmp_path, HERE, [pr])

    assert _run(tmp_path) == 0


@pytest.mark.skipif(_Category is None, reason="conformance package not installed")
def test_the_category_literal_matches_the_classifier() -> None:
    """The scanner writes the category this alarm filters on. If the two drift,
    no PR ever matches and the alarm goes quiet for good."""
    assert _Category is not None
    assert alarm.LOCK_MAINTENANCE == _Category.LOCK_MAINTENANCE.value


def test_the_dashboard_runs_it_on_this_repo() -> None:
    dashboard = job(load_workflow("renovate-dashboard.yaml"), "renovate-dashboard")
    step = step_named(dashboard, "Alarm on a stale lock-maintenance PR")
    assert "renovate_stale_lock_alarm.py" in step["run"]
    assert '--fail-repo "$GITHUB_REPOSITORY"' in step["run"]
    # The refusal alarm runs first and can fail the job; this one must still run.
    assert "!cancelled()" in step["if"]
