"""Tests for .github/scripts/poll_check_runs_gate.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import poll_check_runs_gate as mod

REPO = "atlanhq/application-sdk"
SHA = "abc123"
NAMES = ["Connector E2E run / atlan-openapi-app", "Connector E2E run / atlan-mysql-app"]


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


# --- the gate log must distinguish "never ran" from "failed" (FND-218) ------


def test_the_failure_annotation_names_each_conclusion(monkeypatch, capsys):
    # Bare names made a connector whose tests genuinely failed
    # indistinguishable from one that was evicted before it got a runner —
    # and those have opposite responses (read the diff vs. re-run).
    def fake_run(cmd, **kwargs):
        runs = [
            {"name": NAMES[0], "status": "completed", "conclusion": "failure"},
            {"name": NAMES[1], "status": "completed", "conclusion": "timed_out"},
        ]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None) is False
    out = capsys.readouterr().out
    assert f"{NAMES[0]} (failure)" in out
    assert f"{NAMES[1]} (timed_out)" in out


def test_a_cancelled_check_gets_the_eviction_explanation(monkeypatch, capsys):
    def fake_run(cmd, **kwargs):
        runs = [
            {"name": NAMES[0], "status": "completed", "conclusion": "cancelled"},
            {"name": NAMES[1], "status": "completed", "conclusion": "success"},
        ]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    # Still blocks: an un-run connector test cannot green the merge.
    assert mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None) is False
    out = capsys.readouterr().out
    assert "cancelled, not failed" in out
    assert NAMES[0] in out
    assert "Re-run rather than triage the diff." in out


def test_no_eviction_explanation_when_nothing_was_cancelled(monkeypatch, capsys):
    def fake_run(cmd, **kwargs):
        runs = [
            {"name": NAMES[0], "status": "completed", "conclusion": "failure"},
            {"name": NAMES[1], "status": "completed", "conclusion": "success"},
        ]
        return _http_response(200, '"v1"', _check_runs_body(runs))

    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None) is False
    assert "cancelled, not failed" not in capsys.readouterr().out


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


# --- stopping early when the SHA stops mattering ---------------------------
#
# A missing check is waited out for the full 130min, because a connector can
# legitimately take two hours to report. The one case where that wait is
# knowably pointless: the dispatch guard declined to dispatch because this SHA
# is no longer the PR's head, so nothing will ever create the checks (FND-696).
# These tests pin the boundary — the wait ends only for a MISSING check on a
# superseded SHA, and never on a doubt.

_HEAD = "d47789e0"


def _route(monkeypatch, *, pr_answer, check_runs):
    """Answer the PR read and the check-run listing separately."""
    seen = {"pulls": 0}

    def fake_run(cmd, **kwargs):
        url = cmd[-1]
        if "/pulls/" in url:
            seen["pulls"] += 1
            return pr_answer
        return _http_response(200, '"v1"', _check_runs_body(check_runs))

    monkeypatch.setattr(mod, "run", fake_run)
    return seen


def test_a_superseded_sha_stops_waiting(monkeypatch):
    _route(
        monkeypatch,
        pr_answer=_http_response(200, None, {"head": {"sha": _HEAD}}),
        check_runs=[],
    )
    with pytest.raises(mod.Superseded) as raised:
        mod.wait_for_checks(
            REPO, SHA, NAMES, pr_number=3322, timeout_seconds=7800, sleep=lambda s: None
        )
    assert raised.value.head == _HEAD


def test_a_superseded_sha_exits_zero_rather_than_failing(monkeypatch):
    """Green without a verdict is normally the bug; here the commit is no longer
    under review, so no verdict is required — and a red on an abandoned run is a
    false alarm that automation reads as a real failure."""
    _route(
        monkeypatch,
        pr_answer=_http_response(200, None, {"head": {"sha": _HEAD}}),
        check_runs=[],
    )
    assert (
        mod.main(
            [
                "--repo",
                REPO,
                "--sha",
                SHA,
                "--names-json",
                json.dumps(NAMES),
                "--pr-number",
                "3322",
            ]
        )
        == 0
    )


def test_the_head_sha_keeps_waiting(monkeypatch):
    _route(
        monkeypatch,
        pr_answer=_http_response(200, None, {"head": {"sha": SHA}}),
        check_runs=[],
    )
    ok = mod.wait_for_checks(
        REPO,
        SHA,
        NAMES,
        pr_number=3322,
        interval_seconds=1,
        timeout_seconds=2,
        sleep=lambda s: None,
    )
    assert ok is False


@pytest.mark.parametrize(
    "pr_answer",
    [
        _http_response(404, None, {"message": "Not Found"}),
        _http_response(403, None, {"message": "Resource not accessible"}),
        _http_response(500, None, {"message": "boom"}),
        _http_response(200, None, {"number": 3322}),
        _completed_raw(200, "<html>proxy error</html>"),
    ],
)
def test_an_unreadable_pr_head_keeps_waiting(monkeypatch, pr_answer):
    """Unknown is not superseded. Being wrong here abandons the wait for checks
    that were genuinely on their way, which loses a real verdict."""
    _route(monkeypatch, pr_answer=pr_answer, check_runs=[])
    ok = mod.wait_for_checks(
        REPO,
        SHA,
        NAMES,
        pr_number=3322,
        interval_seconds=1,
        timeout_seconds=2,
        sleep=lambda s: None,
    )
    assert ok is False


def test_a_pending_check_is_never_abandoned(monkeypatch):
    """The distinction that matters. A check that EXISTS has a connector run
    behind it that will report; only a check that was never created can be known
    to be never coming. A superseded SHA whose checks are all present must still
    be waited out, or a real verdict is thrown away."""
    seen = _route(
        monkeypatch,
        pr_answer=_http_response(200, None, {"head": {"sha": _HEAD}}),
        check_runs=[{"name": n, "status": "in_progress"} for n in NAMES],
    )
    ok = mod.wait_for_checks(
        REPO,
        SHA,
        NAMES,
        pr_number=3322,
        interval_seconds=1,
        timeout_seconds=2,
        sleep=lambda s: None,
    )
    assert ok is False
    assert seen["pulls"] == 0, "a pending check must not even ask about the head"


def test_no_pr_number_never_asks(monkeypatch):
    """The merge_group path, unchanged: a queue entry's SHA is not any PR's head,
    so asking would spend a call to learn that a 404 means "carry on"."""
    seen = _route(
        monkeypatch,
        pr_answer=_http_response(200, None, {"head": {"sha": _HEAD}}),
        check_runs=[],
    )
    ok = mod.wait_for_checks(
        REPO, SHA, NAMES, interval_seconds=1, timeout_seconds=2, sleep=lambda s: None
    )
    assert ok is False
    assert seen["pulls"] == 0


def test_the_head_is_re_checked_periodically_not_every_poll(monkeypatch):
    """A push can land mid-wait, so the question is asked again — but at one call
    per five minutes, not one per poll: this token's budget is 1000 requests an
    hour for the whole repository."""
    seen = _route(
        monkeypatch,
        pr_answer=_http_response(200, None, {"head": {"sha": SHA}}),
        check_runs=[],
    )
    mod.wait_for_checks(
        REPO,
        SHA,
        NAMES,
        pr_number=3322,
        interval_seconds=1,
        timeout_seconds=25,
        sleep=lambda s: None,
    )
    # Attempt 1, then every tenth: 1, 10, 20 out of 25 attempts.
    assert seen["pulls"] == 3


# --- wiring: the caller has to pass it, and be allowed to ------------------


def _connector_gate() -> dict:
    workflow = Path(__file__).resolve().parents[2] / "workflows" / "pull_request.yaml"
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][
        "connector-gate"
    ]


def _poll_step(job: dict) -> dict:
    for step in job["steps"]:
        if "poll_check_runs_gate.py" in str(step.get("run", "")):
            return step
    raise AssertionError("connector-gate no longer polls check runs")


def test_the_gate_passes_the_pr_number_from_the_event() -> None:
    """From the event, not an input: the number has to belong to the same PR as
    the SHA being polled, or the early exit would fire against a stranger's
    commit. Empty on merge_group, which is what leaves that path unchanged."""
    step = _poll_step(_connector_gate())
    assert step["env"]["PR_NUMBER"] == "${{ github.event.pull_request.number }}"
    assert "--pr-number" in step["run"], (
        "without the flag the early exit is silently off and a superseded run "
        "waits out the full 130min for checks nobody will ever create"
    )


def test_the_gate_may_read_the_pull_request() -> None:
    """A job-level permissions block REPLACES the workflow default rather than
    merging with it, so dropping this does not fail loudly — the read 403s, the
    poll warns, and it waits the full budget exactly as it used to."""
    assert _connector_gate()["permissions"]["pull-requests"] == "read"


# --- newest-wins among same-named check runs ------------------------------
#
# A retried commit carries MORE THAN ONE check run per name: create_check_run.py
# POSTs a fresh one per dispatch instead of PATCHing the previous attempt, so
# re-running the claiming run (the supported same-commit retry — see
# e2e_dispatch_guard.py's is_my_run) leaves the old attempt's check alongside the
# new one. The gate used to keep whichever the API listed LAST, and that
# endpoint's ordering is undocumented, so the verdict it reported was a coin
# flip between the two attempts. Both listing orders are pinned below, because
# either one alone passes with the broken implementation half the time.

STALE_FAIL = {
    "name": NAMES[0],
    "status": "completed",
    "conclusion": "failure",
    "started_at": "2026-08-21T00:53:41Z",
    "id": 100,
}
FRESH_PASS = {
    "name": NAMES[0],
    "status": "completed",
    "conclusion": "success",
    "started_at": "2026-08-21T01:41:00Z",
    "id": 200,
}
OTHER_LEG_PASS = {
    "name": NAMES[1],
    "status": "completed",
    "conclusion": "success",
    "started_at": "2026-08-21T01:41:00Z",
    "id": 201,
}


@pytest.mark.parametrize(
    "newest_first", [False, True], ids=["oldest-first", "newest-first"]
)
def test_wait_for_checks_keeps_the_newest_of_two_same_named_checks(
    monkeypatch, newest_first
):
    listing = [FRESH_PASS, STALE_FAIL] if newest_first else [STALE_FAIL, FRESH_PASS]
    monkeypatch.setattr(
        mod,
        "run",
        lambda cmd, **kw: _http_response(
            200, '"v1"', _check_runs_body([*listing, OTHER_LEG_PASS])
        ),
    )

    assert (
        mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None) is True
    ), "the superseded attempt's failure was reported over the retry that passed"


@pytest.mark.parametrize(
    "newest_first", [False, True], ids=["oldest-first", "newest-first"]
)
def test_wait_for_checks_does_not_let_a_stale_pass_mask_a_fresh_failure(
    monkeypatch, newest_first
):
    """The other direction, which matters more: newest-wins must not have been
    implemented as "prefer the passing one"."""
    stale_pass = {**STALE_FAIL, "conclusion": "success"}
    fresh_fail = {**FRESH_PASS, "conclusion": "failure"}
    listing = [fresh_fail, stale_pass] if newest_first else [stale_pass, fresh_fail]
    monkeypatch.setattr(
        mod,
        "run",
        lambda cmd, **kw: _http_response(
            200, '"v1"', _check_runs_body([*listing, OTHER_LEG_PASS])
        ),
    )

    assert (
        mod.wait_for_checks(REPO, SHA, NAMES, sleep=lambda s: None) is False
    ), "a superseded pass masked the retry's failure"


def test_wait_for_checks_waits_when_the_newest_attempt_is_still_running(monkeypatch):
    """A concluded older attempt must not satisfy the gate while the retry that
    superseded it is still in flight — that declares a verdict from a run nobody
    is waiting on any more.

    The stale attempt is the PASSING one and is listed last, so the old
    last-wins code returns True here immediately. Making it the failing one
    instead would give False either way and prove nothing.
    """
    stale_pass = {**STALE_FAIL, "conclusion": "success"}
    running_retry = {
        "name": NAMES[0],
        "status": "in_progress",
        "started_at": "2026-08-21T01:41:00Z",
        "id": 200,
    }
    monkeypatch.setattr(
        mod,
        "run",
        lambda cmd, **kw: _http_response(
            200, '"v1"', _check_runs_body([running_retry, stale_pass, OTHER_LEG_PASS])
        ),
    )

    assert (
        mod.wait_for_checks(
            REPO,
            SHA,
            NAMES,
            interval_seconds=1,
            timeout_seconds=2,
            sleep=lambda s: None,
        )
        is False
    ), "a superseded pass satisfied the gate while the retry was still running"


def test_check_run_age_key_breaks_ties_on_id_and_tolerates_no_timestamp():
    same_second_old = {"name": NAMES[0], "started_at": "2026-08-21T01:41:00Z", "id": 1}
    same_second_new = {"name": NAMES[0], "started_at": "2026-08-21T01:41:00Z", "id": 2}
    assert mod.check_run_age_key(same_second_new) > mod.check_run_age_key(
        same_second_old
    )
    # A check run with no started_at must still be orderable rather than blow up
    # the whole poll on a TypeError comparing None to str.
    assert mod.check_run_age_key({"name": NAMES[0], "id": 5}) == ("", 5)
    assert mod.check_run_age_key({"name": NAMES[0]}) == ("", 0)


def test_remember_newest_is_the_only_way_latest_is_populated():
    """Guards the fix itself. Every behavioural test above still passes if only
    ONE of the two assignment sites is converted, so a future edit that
    re-introduces a bare ``latest[name] = check_run`` in either branch would
    silently restore the coin flip."""
    source = (Path(__file__).parent.parent / "poll_check_runs_gate.py").read_text()
    poll_body = source.split("def wait_for_checks(", 1)[1]
    assert "latest[check_run[" not in poll_body, (
        "wait_for_checks assigns into `latest` directly again — route it through "
        "remember_newest() so same-named check runs stay newest-wins"
    )
    assert (
        poll_body.count("remember_newest(latest, check_run)") == 2
    ), "both the conditional-GET and the full-pagination branch must dedupe"
