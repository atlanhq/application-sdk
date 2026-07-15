"""Tests for .github/scripts/poll_check_runs_gate.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import poll_check_runs_gate as mod

REPO = "atlanhq/application-sdk"
SHA = "abc123"
NAMES = ["Connector E2E / atlan-openapi-app", "Connector E2E / atlan-mysql-app"]


@pytest.fixture(autouse=True)
def _gh_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")


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


def _ndjson(runs: list[dict]) -> subprocess.CompletedProcess:
    """What `gh api --paginate --jq '.check_runs[] | tojson'` produces: one
    compact JSON object per line, across however many pages it took."""
    text = "\n".join(json.dumps(r) for r in runs) + ("\n" if runs else "")
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=text, stderr="")


def _completed_raw(status: int, body_text: str) -> subprocess.CompletedProcess:
    """Like _http_response, but for a raw (non-JSON-dict) body string —
    e.g. a GitHub error response's raw text."""
    raw = f"HTTP/2.0 {status} whatever\n\n{body_text}"
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=raw, stderr="")


def test_gh_api_conditional_parses_200_and_etag(monkeypatch):
    monkeypatch.setattr(
        mod, "run", lambda cmd, **kw: _http_response(200, '"v1"', {"check_runs": []})
    )

    status, etag, body = mod.gh_api_conditional("some/path")
    assert status == 200
    assert etag == '"v1"'
    assert body == {"check_runs": []}


def test_gh_api_conditional_304_has_no_body_and_keeps_prior_etag(monkeypatch):
    # Regression test for a real production failure (merge-queue run
    # 28949755456): the original implementation shelled out to `gh api`,
    # which treats a 304 as a command failure (exits non-zero, prints
    # "gh: HTTP 304" instead of the response) — indistinguishable from a
    # genuine error. curl without --fail returns 0 and the real response
    # for ANY status code, so a 304 must parse cleanly here, not raise.
    monkeypatch.setattr(mod, "run", lambda cmd, **kw: _http_response(304, None, None))

    status, etag, body = mod.gh_api_conditional("some/path", etag='"v1"')
    assert status == 304
    assert etag == '"v1"'
    assert body is None


def test_gh_api_conditional_uses_curl_not_gh(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _http_response(200, '"v1"', {"check_runs": []})

    monkeypatch.setattr(mod, "run", fake_run)
    mod.gh_api_conditional("some/path")

    assert captured["cmd"][0] == "curl"
    assert "gh" not in captured["cmd"]
    assert any("Authorization: Bearer test-token" == p for p in captured["cmd"])
    assert captured["cmd"][-1] == "https://api.github.com/some/path"


def test_gh_api_conditional_requires_a_token(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    try:
        mod.gh_api_conditional("some/path")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "GH_TOKEN" in str(e)


def test_gh_api_conditional_raises_on_transport_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=28, stdout="", stderr="curl: (28) Operation timed out"
        )

    monkeypatch.setattr(mod, "run", fake_run)

    try:
        mod.gh_api_conditional("some/path")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "Operation timed out" in str(e)


def test_gh_api_conditional_raises_on_http_error_status(monkeypatch):
    monkeypatch.setattr(
        mod,
        "run",
        lambda cmd, **kw: _completed_raw(403, '{"message": "API rate limit exceeded"}'),
    )

    try:
        mod.gh_api_conditional("some/path")
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "403" in str(e)
        assert "API rate limit exceeded" in str(e)


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


def test_list_all_check_runs_uses_paginate_and_parses_ndjson(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ndjson([{"id": 1, "name": NAMES[0]}, {"id": 2, "name": NAMES[1]}])

    monkeypatch.setattr(mod, "run", fake_run)

    runs = mod.list_all_check_runs(REPO, SHA)

    assert captured["cmd"][0] == "gh"
    assert "--paginate" in captured["cmd"]
    assert runs == [{"id": 1, "name": NAMES[0]}, {"id": 2, "name": NAMES[1]}]


def test_list_all_check_runs_raises_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="rate limited"
        )

    monkeypatch.setattr(mod, "run", fake_run)

    try:
        mod.list_all_check_runs(REPO, SHA)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "rate limited" in str(e)


def test_wait_for_checks_falls_back_to_full_pagination_when_truncated(monkeypatch):
    # total_count exceeds what a single per_page=100 page returns — ETag
    # caching can't safely span multiple pages (a page-1 match doesn't
    # prove page 2 is unchanged), so this must switch to a full, uncached
    # fetch and still resolve correctly rather than fail the gate closed.
    calls = {"curl": 0, "gh": 0}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "curl":
            calls["curl"] += 1
            body = {
                "total_count": 150,
                "check_runs": [
                    {"name": NAMES[0], "status": "completed", "conclusion": "success"}
                ],
            }
            return _http_response(200, '"v1"', body)
        calls["gh"] += 1
        # The full-pagination fallback sees the complete picture, including
        # the second connector's check run that page 1 alone couldn't.
        runs = [
            {"name": n, "status": "completed", "conclusion": "success"} for n in NAMES
        ]
        return _ndjson(runs)

    monkeypatch.setattr(mod, "run", fake_run)

    ok = mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None)

    assert ok is True
    assert calls["curl"] == 1  # only the first attempt tries the cheap conditional path
    assert calls["gh"] == 1


def test_wait_for_checks_stays_in_full_pagination_mode_across_attempts(monkeypatch):
    calls = {"curl": 0, "gh": 0}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "curl":
            calls["curl"] += 1
            body = {
                "total_count": 150,
                "check_runs": [{"name": NAMES[0], "status": "in_progress"}],
            }
            return _http_response(200, '"v1"', body)
        calls["gh"] += 1
        if calls["gh"] < 2:
            runs = [{"name": NAMES[0], "status": "in_progress"}]
        else:
            runs = [
                {"name": n, "status": "completed", "conclusion": "success"}
                for n in NAMES
            ]
        return _ndjson(runs)

    monkeypatch.setattr(mod, "run", fake_run)

    ok = mod.wait_for_checks(
        REPO, SHA, NAMES, interval_seconds=1, timeout_seconds=10, sleep=lambda s: None
    )

    assert ok is True
    # Only the very first attempt uses the cheap conditional path; every
    # subsequent attempt (including the one that resolves) stays in full
    # pagination mode rather than falling back to (incorrect) caching.
    assert calls["curl"] == 1
    assert calls["gh"] == 2


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


def test_wait_for_checks_ceiling_divides_timeout_into_attempts(monkeypatch):
    # timeout=31s / interval=30s must allow 2 attempts, not floor-truncate
    # to 1 — the second attempt is what completes here, so this fails under
    # floor division (which would give up after the first).
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            runs = [{"name": NAMES[0], "status": "in_progress"}]
        else:
            runs = [{"name": NAMES[0], "status": "completed", "conclusion": "success"}]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    ok = mod.wait_for_checks(
        REPO,
        SHA,
        [NAMES[0]],
        interval_seconds=30,
        timeout_seconds=31,
        sleep=lambda s: None,
    )
    assert ok is True
    assert calls["n"] == 2


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
