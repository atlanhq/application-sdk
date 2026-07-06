"""Tests for .github/scripts/complete_check_run.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import complete_check_run as mod

REPO = "atlanhq/application-sdk"
SHA = "abc123"
NAME = "Connector E2E / atlan-mysql-app"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_find_check_run_picks_highest_id_on_duplicates():
    runs = [
        {"id": 1, "name": NAME},
        {"id": 5, "name": NAME},
        {"id": 3, "name": "Connector E2E / atlan-openapi-app"},
    ]
    found = mod.find_check_run(runs, NAME)
    assert found["id"] == 5


def test_find_check_run_returns_none_when_absent():
    assert mod.find_check_run([{"id": 1, "name": "other"}], NAME) is None


def test_truncate_summary_short_passthrough():
    assert mod.truncate_summary("hello") == "hello"


def test_truncate_summary_long_gets_truncated():
    long = "x" * (mod.MAX_SUMMARY_CHARS + 100)
    result = mod.truncate_summary(long)
    assert len(result) <= mod.MAX_SUMMARY_CHARS + len("\n\n… (truncated)")
    assert result.endswith("(truncated)")


def test_complete_check_run_patches_existing(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        if cmd[0:2] == ["gh", "api"] and "check-runs?per_page=100" in cmd[2]:
            return _completed(json.dumps({"check_runs": [{"id": 55, "name": NAME}]}))
        return _completed("{}")

    monkeypatch.setattr(mod, "run", fake_run)

    mod.complete_check_run(REPO, SHA, NAME, "success", "All good")

    patch_call = next(
        c for c in calls if c[0][:3] == ["gh", "api", "--method"] and c[0][3] == "PATCH"
    )
    assert patch_call[0][4] == f"repos/{REPO}/check-runs/55"
    payload = json.loads(patch_call[1])
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "success"
    assert payload["output"]["summary"] == "All good"


def test_complete_check_run_creates_when_missing(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        if "check-runs?per_page=100" in cmd[2]:
            return _completed(json.dumps({"check_runs": []}))
        return _completed("{}")

    monkeypatch.setattr(mod, "run", fake_run)

    mod.complete_check_run(REPO, SHA, NAME, "failure", "Broke")

    post_call = next(
        c for c in calls if c[0][:3] == ["gh", "api", "--method"] and c[0][3] == "POST"
    )
    assert post_call[0][4] == f"repos/{REPO}/check-runs"
    payload = json.loads(post_call[1])
    assert payload["conclusion"] == "failure"


def test_complete_check_run_raises_on_patch_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        if "check-runs?per_page=100" in cmd[2]:
            return _completed(json.dumps({"check_runs": [{"id": 1, "name": NAME}]}))
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="nope"
        )

    monkeypatch.setattr(mod, "run", fake_run)

    try:
        mod.complete_check_run(REPO, SHA, NAME, "success", "x")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "nope" in str(e)


def test_main_reads_summary_file(monkeypatch, tmp_path):
    summary_file = tmp_path / "body.md"
    summary_file.write_text("rendered report")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        if "check-runs?per_page=100" in cmd[2]:
            return _completed(json.dumps({"check_runs": [{"id": 9, "name": NAME}]}))
        return _completed("{}")

    monkeypatch.setattr(mod, "run", fake_run)
    rc = mod.main(
        [
            "--repo",
            REPO,
            "--sha",
            SHA,
            "--name",
            NAME,
            "--conclusion",
            "success",
            "--summary-file",
            str(summary_file),
        ]
    )
    assert rc == 0
    patch_call = next(c for c in calls if len(c[0]) > 3 and c[0][3] == "PATCH")
    assert json.loads(patch_call[1])["output"]["summary"] == "rendered report"


def test_main_missing_summary_file_errors(monkeypatch):
    monkeypatch.setattr(mod, "run", lambda *a, **k: _completed("{}"))
    try:
        mod.main(
            [
                "--repo",
                REPO,
                "--sha",
                SHA,
                "--name",
                NAME,
                "--conclusion",
                "success",
                "--summary-file",
                "/no/such/file",
            ]
        )
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "could not read" in str(e)
