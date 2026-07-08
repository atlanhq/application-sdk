"""Tests for .github/scripts/create_check_run.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import create_check_run as mod

REPO = "atlanhq/application-sdk"
SHA = "abc123"
NAME = "Connector E2E / atlan-mysql-app"


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_create_check_run_posts_in_progress_and_returns_id(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["payload"] = json.loads(kwargs["input"])
        return _completed(json.dumps({"id": 4242}))

    monkeypatch.setattr(mod, "run", fake_run)

    check_run_id = mod.create_check_run(
        REPO, SHA, NAME, summary="Dispatched; awaiting results."
    )

    assert check_run_id == 4242
    assert captured["cmd"][:4] == ["gh", "api", "--method", "POST"]
    assert captured["cmd"][4] == f"repos/{REPO}/check-runs"
    payload = captured["payload"]
    assert payload["name"] == NAME
    assert payload["head_sha"] == SHA
    assert payload["status"] == "in_progress"
    assert payload["output"]["summary"] == "Dispatched; awaiting results."
    assert payload["output"]["title"] == NAME
    assert "details_url" not in payload


def test_create_check_run_includes_details_url_and_title(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["payload"] = json.loads(kwargs["input"])
        return _completed(json.dumps({"id": 1}))

    monkeypatch.setattr(mod, "run", fake_run)

    mod.create_check_run(
        REPO,
        SHA,
        NAME,
        title="Custom Title",
        summary="x",
        details_url="https://example.com/run/1",
    )

    assert captured["payload"]["output"]["title"] == "Custom Title"
    assert captured["payload"]["details_url"] == "https://example.com/run/1"


def test_create_check_run_raises_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom"
        )

    monkeypatch.setattr(mod, "run", fake_run)

    try:
        mod.create_check_run(REPO, SHA, NAME, summary="x")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "boom" in str(e)


def test_main_writes_github_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return _completed(json.dumps({"id": 99}))

    monkeypatch.setattr(mod, "run", fake_run)
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    rc = mod.main(["--repo", REPO, "--sha", SHA, "--name", NAME, "--summary", "x"])

    assert rc == 0
    assert output_file.read_text() == "check_run_id=99\n"


def test_main_prints_when_no_github_output(monkeypatch, capsys):
    def fake_run(cmd, **kwargs):
        return _completed(json.dumps({"id": 7}))

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    rc = mod.main(["--repo", REPO, "--sha", SHA, "--name", NAME, "--summary", "x"])

    assert rc == 0
    assert "check_run_id=7" in capsys.readouterr().out
