"""Tests for .github/scripts/dispatch_workflow.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import dispatch_workflow as mod

REPO = "atlanhq/atlan-mysql-app"
WORKFLOW = "tests.yaml"
REF = "refs/heads/main"


def _completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="", stderr=stderr
    )


def test_dispatch_posts_ref_and_inputs(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["payload"] = json.loads(kwargs["input"])
        return _completed()

    monkeypatch.setattr(mod, "run", fake_run)

    mod.dispatch(
        REPO, WORKFLOW, REF, {"application_sdk_ref": "abc123", "run_e2e": "true"}
    )

    assert captured["cmd"][:4] == ["gh", "api", "--method", "POST"]
    assert captured["cmd"][4] == f"repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches"
    assert captured["payload"] == {
        "ref": REF,
        "inputs": {"application_sdk_ref": "abc123", "run_e2e": "true"},
    }


def test_dispatch_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        mod, "run", lambda cmd, **kw: _completed(returncode=1, stderr="boom")
    )

    try:
        mod.dispatch(REPO, WORKFLOW, REF, {})
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "boom" in str(e)


def test_main_rejects_invalid_json(monkeypatch):
    try:
        mod.main(
            [
                "--repo",
                REPO,
                "--workflow",
                WORKFLOW,
                "--ref",
                REF,
                "--inputs",
                "not json",
            ]
        )
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "not valid JSON" in str(e)


def test_main_rejects_non_object_json(monkeypatch):
    try:
        mod.main(
            ["--repo", REPO, "--workflow", WORKFLOW, "--ref", REF, "--inputs", "[1, 2]"]
        )
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "must be a JSON object" in str(e)


def test_main_success(monkeypatch, capsys):
    monkeypatch.setattr(mod, "run", lambda cmd, **kw: _completed())

    rc = mod.main(
        ["--repo", REPO, "--workflow", WORKFLOW, "--ref", REF, "--inputs", "{}"]
    )

    assert rc == 0
    assert f"Dispatched {WORKFLOW} on {REPO}@{REF}" in capsys.readouterr().out
