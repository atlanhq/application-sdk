"""Tests for .github/scripts/update_check_run_details_url.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import update_check_run_details_url as mod

REPO = "atlanhq/application-sdk"


def _completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="{}", stderr=stderr
    )


def test_update_details_url_patches_only_that_field(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["payload"] = json.loads(kwargs["input"])
        return _completed()

    monkeypatch.setattr(mod, "run", fake_run)

    mod.update_details_url(REPO, 42, "https://example.com/run/1")

    assert captured["cmd"][:4] == ["gh", "api", "--method", "PATCH"]
    assert captured["cmd"][4] == f"repos/{REPO}/check-runs/42"
    assert captured["payload"] == {"details_url": "https://example.com/run/1"}


def test_update_details_url_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        mod, "run", lambda cmd, **kw: _completed(returncode=1, stderr="not found")
    )

    try:
        mod.update_details_url(REPO, 42, "https://example.com/run/1")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "not found" in str(e)


def test_main_success(monkeypatch, capsys):
    monkeypatch.setattr(mod, "run", lambda cmd, **kw: _completed())

    rc = mod.main(
        [
            "--repo",
            REPO,
            "--check-run-id",
            "42",
            "--details-url",
            "https://example.com/run/1",
        ]
    )

    assert rc == 0
    assert "Updated details_url on check run 42" in capsys.readouterr().out
