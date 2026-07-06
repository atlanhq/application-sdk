"""Tests for .github/scripts/poll_check_runs_gate.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import poll_check_runs_gate as mod

REPO = "atlanhq/application-sdk"
SHA = "abc123"
NAMES = ["Connector E2E / atlan-openapi-app", "Connector E2E / atlan-mysql-app"]


def _http_response(
    status: int, etag: str | None, body: dict | None
) -> subprocess.CompletedProcess:
    headers = [f"HTTP/2.0 {status} whatever"]
    if etag:
        headers.append(f"ETag: {etag}")
    body_text = json.dumps(body) if body is not None else ""
    raw = "\n".join(headers) + "\n\n" + body_text
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=raw, stderr="")


def _check_runs_body(runs: list[dict]) -> dict:
    return {"total_count": len(runs), "check_runs": runs}


def test_gh_api_conditional_parses_200_and_etag(monkeypatch):
    monkeypatch.setattr(
        mod, "run", lambda cmd, **kw: _http_response(200, '"v1"', {"check_runs": []})
    )

    status, etag, body = mod.gh_api_conditional("some/path")
    assert status == 200
    assert etag == '"v1"'
    assert body == {"check_runs": []}


def test_gh_api_conditional_304_has_no_body_and_keeps_prior_etag(monkeypatch):
    monkeypatch.setattr(mod, "run", lambda cmd, **kw: _http_response(304, None, None))

    status, etag, body = mod.gh_api_conditional("some/path", etag='"v1"')
    assert status == 304
    assert etag == '"v1"'
    assert body is None


def test_wait_for_checks_succeeds_immediately_when_all_pass(monkeypatch):
    def fake_run(cmd, **kwargs):
        runs = [
            {"name": n, "status": "completed", "conclusion": "success"} for n in NAMES
        ]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    ok = mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None)
    assert ok is True


def test_wait_for_checks_polls_until_completed(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            runs = [{"name": NAMES[0], "status": "in_progress"}]
            return _http_response(200, '"v1"', _check_runs_body(runs))
        runs = [
            {"name": n, "status": "completed", "conclusion": "success"} for n in NAMES
        ]
        return _http_response(200, '"v2"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    ok = mod.wait_for_checks(
        REPO, SHA, NAMES, interval_seconds=1, timeout_seconds=10, sleep=lambda s: None
    )
    assert ok is True
    assert calls["n"] == 3


def test_wait_for_checks_uses_304_cache_without_losing_state(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            runs = [
                {"name": n, "status": "completed", "conclusion": "success"}
                for n in NAMES
            ]
            return _http_response(200, '"v1"', _check_runs_body(runs))
        # Every subsequent poll is a 304 — nothing changed, but state from the
        # first successful call must still be treated as authoritative.
        return _http_response(304, None, None)

    monkeypatch.setattr(mod, "run", fake_run)
    ok = mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None)
    assert ok is True
    assert calls["n"] == 1  # loop breaks right after the first (complete) poll


def test_wait_for_checks_fails_on_bad_conclusion(monkeypatch):
    def fake_run(cmd, **kwargs):
        runs = [
            {"name": NAMES[0], "status": "completed", "conclusion": "failure"},
            {"name": NAMES[1], "status": "completed", "conclusion": "success"},
        ]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    ok = mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None)
    assert ok is False


def test_wait_for_checks_times_out_when_never_complete(monkeypatch):
    def fake_run(cmd, **kwargs):
        runs = [{"name": NAMES[0], "status": "in_progress"}]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    ok = mod.wait_for_checks(
        REPO, SHA, NAMES, interval_seconds=1, timeout_seconds=3, sleep=lambda s: None
    )
    assert ok is False


def test_main_exit_codes(monkeypatch):
    def fake_run_pass(cmd, **kwargs):
        runs = [
            {"name": n, "status": "completed", "conclusion": "success"} for n in NAMES
        ]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run_pass)
    rc = mod.main(
        ["--repo", REPO, "--sha", SHA, "--name", NAMES[0], "--name", NAMES[1]]
    )
    assert rc == 0

    def fake_run_fail(cmd, **kwargs):
        runs = [{"name": NAMES[0], "status": "completed", "conclusion": "failure"}]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run_fail)
    rc = mod.main(
        [
            "--repo",
            REPO,
            "--sha",
            SHA,
            "--name",
            NAMES[0],
            "--interval-seconds",
            "1",
            "--timeout-seconds",
            "1",
        ]
    )
    assert rc == 1


def test_main_accepts_names_json(monkeypatch):
    def fake_run(cmd, **kwargs):
        runs = [
            {"name": n, "status": "completed", "conclusion": "success"} for n in NAMES
        ]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    rc = mod.main(["--repo", REPO, "--sha", SHA, "--names-json", json.dumps(NAMES)])
    assert rc == 0


def test_main_rejects_invalid_names_json(monkeypatch):
    try:
        mod.main(["--repo", REPO, "--sha", SHA, "--names-json", "not json"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "not valid JSON" in str(e)


def test_main_rejects_non_list_names_json(monkeypatch):
    try:
        mod.main(["--repo", REPO, "--sha", SHA, "--names-json", '{"a": 1}'])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "must be a JSON array" in str(e)


def test_main_rejects_both_name_and_names_json(monkeypatch):
    try:
        mod.main(
            ["--repo", REPO, "--sha", SHA, "--name", NAMES[0], "--names-json", "[]"]
        )
        assert False, "expected SystemExit (argparse mutually exclusive)"
    except SystemExit:
        pass
