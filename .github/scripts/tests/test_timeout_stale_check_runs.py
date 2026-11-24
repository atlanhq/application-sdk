"""Tests for .github/scripts/timeout_stale_check_runs.py."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import _gh
import timeout_stale_check_runs as mod

REPO = "atlanhq/application-sdk"
NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)
PREFIX = "Connector E2E run / "


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _pr_pages(prs: list[dict]) -> subprocess.CompletedProcess:
    """What `gh api --paginate --slurp .../pulls` prints: an array of pages."""
    return _completed(json.dumps([prs]))


def _check_run_pages(runs: list[dict]) -> subprocess.CompletedProcess:
    """What `gh api --paginate --slurp .../check-runs` prints: a JSON array
    holding every page; one page is enough for these tests."""
    return _completed(json.dumps([{"total_count": len(runs), "check_runs": runs}]))


def _is_open_prs_call(cmd: list[str]) -> bool:
    return any("pulls?state=open" in part for part in cmd)


def _is_check_runs_call(cmd: list[str]) -> bool:
    return any("check-runs?per_page=100" in part for part in cmd)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_is_stale_only_flags_old_in_progress_runs():
    stale = {"status": "in_progress", "started_at": _iso(NOW - timedelta(minutes=200))}
    fresh = {"status": "in_progress", "started_at": _iso(NOW - timedelta(minutes=5))}
    done = {"status": "completed", "started_at": _iso(NOW - timedelta(minutes=200))}
    max_age = timedelta(minutes=130)

    assert mod.is_stale(stale, max_age, NOW) is True
    assert mod.is_stale(fresh, max_age, NOW) is False
    assert mod.is_stale(done, max_age, NOW) is False


def test_is_stale_handles_missing_started_at():
    assert mod.is_stale({"status": "in_progress"}, timedelta(minutes=1), NOW) is False


def test_list_open_prs_uses_paginate(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _pr_pages([{"number": 1, "head": {"sha": "s"}}])

    monkeypatch.setattr(_gh, "run", fake_run)
    prs = mod.list_open_prs(REPO)

    assert "--paginate" in captured["cmd"]
    assert prs == [{"number": 1, "head": {"sha": "s"}}]


def test_list_check_runs_uses_paginate(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _check_run_pages([{"id": 1, "name": "x"}])

    monkeypatch.setattr(_gh, "run", fake_run)
    runs = mod.list_check_runs(REPO, "sha1")

    assert "--paginate" in captured["cmd"]
    assert runs == [{"id": 1, "name": "x"}]


def test_sweep_times_out_only_stale_matching_checks(monkeypatch):
    prs = [{"number": 42, "head": {"sha": "sha1"}}]
    check_runs = [
        {
            "id": 1,
            "name": f"{PREFIX}atlan-openapi-app",
            "status": "in_progress",
            "started_at": _iso(NOW - timedelta(minutes=200)),
        },
        {
            "id": 2,
            "name": f"{PREFIX}atlan-mysql-app",
            "status": "in_progress",
            "started_at": _iso(NOW - timedelta(minutes=5)),
        },
        {
            "id": 3,
            "name": "Unrelated Check",
            "status": "in_progress",
            "started_at": _iso(NOW - timedelta(minutes=999)),
        },
    ]
    patched = []

    def fake_run(cmd, **kwargs):
        if _is_open_prs_call(cmd):
            return _pr_pages(prs)
        if _is_check_runs_call(cmd):
            return _check_run_pages(check_runs)
        if cmd[:3] == ["gh", "api", "--method"]:
            patched.append((cmd[4], json.loads(kwargs["input"])))
            return _completed("{}")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(_gh, "run", fake_run)
    timed_out = mod.sweep(REPO, PREFIX, 130, now=NOW)

    assert timed_out == [f"{PREFIX}atlan-openapi-app (PR #42)"]
    assert len(patched) == 1
    endpoint, payload = patched[0]
    assert endpoint == f"repos/{REPO}/check-runs/1"
    assert payload["conclusion"] == "timed_out"
    assert payload["status"] == "completed"


def test_sweep_returns_empty_when_nothing_stale(monkeypatch):
    def fake_run(cmd, **kwargs):
        if _is_open_prs_call(cmd):
            return _pr_pages([])
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(_gh, "run", fake_run)
    assert mod.sweep(REPO, PREFIX, 130, now=NOW) == []


def test_list_open_prs_raises_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="rate limited"
        )

    monkeypatch.setattr(_gh, "run", fake_run)
    try:
        mod.list_open_prs(REPO)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "rate limited" in str(e)


def test_main_reports_timed_out_runs(monkeypatch, capsys):
    prs = [{"number": 1, "head": {"sha": "s"}}]
    check_runs = [
        {
            "id": 9,
            "name": f"{PREFIX}atlan-mysql-app",
            "status": "in_progress",
            "started_at": _iso(NOW - timedelta(minutes=999)),
        }
    ]

    def fake_run(cmd, **kwargs):
        if _is_open_prs_call(cmd):
            return _pr_pages(prs)
        if _is_check_runs_call(cmd):
            return _check_run_pages(check_runs)
        return _completed("{}")

    monkeypatch.setattr(_gh, "run", fake_run)
    rc = mod.main(["--repo", REPO])
    assert rc == 0
    assert "Timed out 1 stuck check run" in capsys.readouterr().out
