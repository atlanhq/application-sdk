"""Tests for .github/scripts/sdk_resolve_dispatch.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sdk_resolve_dispatch as sr


def _stream(*lines: str):
    return sr.process_stream(list(lines))


# ---------------------------------------------------------------------------
# payload / prompt
# ---------------------------------------------------------------------------


def test_payload_shape():
    p = sr.build_payload(
        "1234",
        "http://run",
        8,
        "2026-07-08",
        "reviewer-one,reviewer-two",
        "requester-login",
    )
    assert p["mode"] == "direct" and p["stream"] is True
    assert p["source_id"] == "sdk-resolve-1234-2026-07-08"
    assert p["repositories"] == ["atlanhq/application-sdk"]
    assert p["metadata"]["pr_number"] == "1234"
    assert p["metadata"]["max_rounds"] == 8
    assert p["metadata"]["reviewers"] == "reviewer-one,reviewer-two"
    assert p["metadata"]["requester"] == "requester-login"


def test_payload_pins_all_three_model_lanes():
    # All three lanes must be pinned: leaving any unset silently falls back to
    # mothership's Claude defaults (main -> claude-opus-5, sub-agent ->
    # claude-sonnet-5), and `small_fast_model` unset resolves to `model`.
    p = sr.build_payload("1", "u", 8, "2026-07-08", "reviewer-one", "requester-login")
    assert p["model"] == "kimi-k3"
    assert p["small_fast_model"] == "gpt-5.6-luna"
    assert p["env_vars"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "gpt-5.6-luna"
    # Every pinned value must survive JSON encoding as a non-blank string —
    # env_vars skips the API's model-id validation that `model` gets.
    encoded = json.loads(json.dumps(p))
    for value in (
        encoded["model"],
        encoded["small_fast_model"],
        encoded["env_vars"]["CLAUDE_CODE_SUBAGENT_MODEL"],
    ):
        assert isinstance(value, str) and value == value.strip() and value


def test_prompt_carries_pr_stop_line_and_review_request():
    prompt = sr.build_prompt(
        "42", "u", 8, "reviewer-one,reviewer-two", "requester-login"
    )
    assert "PR_NUMBER:    42" in prompt
    assert "MERGE-READY" in prompt
    assert "Do NOT `gh pr merge`" in prompt  # human merges
    assert "pr-resolve/ORCHESTRATION.md" in prompt
    # requests human review + tags reviewers AND the requester
    assert "gh pr edit 42 --add-reviewer reviewer-one,reviewer-two" in prompt
    assert (
        "@reviewer-one" in prompt
        and "@reviewer-two" in prompt
        and "@requester-login" in prompt
    )


def test_prompt_without_configured_reviewers_asks_for_a_human_assignment():
    # REVIEWERS comes from a repo variable and may be unset. The prompt must not
    # emit an argument-less `gh pr edit --add-reviewer`, which would fail — and
    # it must not silently skip the hand-off either: the missing list is named
    # in the report so someone assigns a reviewer.
    prompt = sr.build_prompt("42", "u", 8, "", "requester-login")
    assert "--add-reviewer" not in prompt
    assert "NO reviewer list is configured" in prompt
    assert "SDK_RESOLVE_REVIEWERS" in prompt
    # the requester is still tagged so someone is pinged
    assert "@requester-login" in prompt
    # whitespace/comma-only values are treated as unset too
    assert "--add-reviewer" not in sr.build_prompt("42", "u", 8, " , ", "")


def test_reviewer_handles_dedupe_and_strip():
    # strips '@', de-dupes, appends requester, drops blanks
    assert sr._reviewer_handles("@reviewer-one, reviewer-two", "requester-login") == [
        "reviewer-one",
        "reviewer-two",
        "requester-login",
    ]
    # requester already in the reviewer list → not duplicated
    assert sr._reviewer_handles("reviewer-one,reviewer-two", "reviewer-one") == [
        "reviewer-one",
        "reviewer-two",
    ]


# ---------------------------------------------------------------------------
# SSE state machine + exit decision
# ---------------------------------------------------------------------------


def test_successful_complete_stream():
    st = _stream(
        "event: started",
        'data: {"session_id": "s1", "sandbox_id": "b1"}',
        "",
        "event: complete",
        'data: {"status": "completed", "cost_usd": "7.50"}',
    )
    assert st.completed and st.status == "completed" and st.cost == "7.50"
    assert sr.decide_exit(st) == (0, "SDK Resolve completed (cost=7.50).")


def test_error_event_fails():
    st = _stream("event: error", 'data: {"code": "boom", "message": "kaboom"}')
    assert st.errored is True
    code, msg = sr.decide_exit(st)
    assert code == 1 and "boom" in msg and "kaboom" in msg
    assert "`boom` kaboom" in sr.render_step_summary(st, "1234", "http://run")


def test_complete_with_error_status_surfaces_code_and_message():
    # Regression: a `complete` carrying status=error used to leave st.errored
    # False, so both consumers dropped the parsed reason and the log showed only
    # "final status=error" with no code/message.
    st = _stream(
        "event: complete",
        'data: {"status": "error", "cost_usd": "1.75", '
        '"error": {"code": "upstream_error", "message": "provider returned 400"}}',
    )
    assert st.errored is True
    code, msg = sr.decide_exit(st)
    assert code == 1
    assert "upstream_error" in msg and "provider returned 400" in msg
    out = sr.render_step_summary(st, "1234", "http://run")
    assert "`upstream_error` provider returned 400" in out


def test_complete_with_error_status_and_no_detail_keeps_status_message():
    # Empty error object: there is nothing to surface, so stay on the bare
    # status message rather than reporting a content-free `code=none`.
    st = _stream("event: complete", 'data: {"status": "error"}')
    assert st.errored is False
    code, msg = sr.decide_exit(st)
    assert code == 1 and "final status=error" in msg


def test_complete_with_error_status_does_not_mask_oob_handoff():
    # A hard error still yields to the out-of-band hand-off row when one is
    # found, exactly as the standalone-`error` path does.
    st = _stream(
        "event: complete",
        'data: {"status": "error", "error": {"code": "c", "message": "m"}}',
    )
    out = sr.render_step_summary(st, "1234", "http://run", "http://comment")
    assert "out-of-band" in out
    assert "**Error:**" not in out


def test_no_events_fails():
    assert sr.decide_exit(_stream())[0] == 1


def test_events_but_no_complete_fails():
    st = _stream("event: action", 'data: {"action_name": "gh pr checkout"}')
    assert sr.decide_exit(st)[0] == 1


def test_no_complete_but_phase4_summary_soft_completes():
    # Stream ends (clean EOF) with no `complete` sentinel, but the resolver
    # streamed its Phase 4 summary block — that is end-of-run evidence, so the
    # run is treated as completed (exit 0) rather than a transport failure.
    fragments = [
        line
        for chunk in _SUMMARY_BLOCK.splitlines(keepends=True)
        for line in ("event: response", f'data: {json.dumps({"text": chunk})}')
    ]
    st = _stream(*fragments)
    assert not st.completed
    code, msg = sr.decide_exit(st)
    assert code == 0 and "treating the run as completed" in msg
    assert sr.run_completed(st)


def test_no_complete_and_no_summary_is_diagnosed_as_midrun_drop():
    # Responses but no summary block and no sentinel → genuinely truncated.
    # Still fails (exit 1), and the message names it a mid-run stream drop
    # rather than a resolver bug.
    st = _stream("event: response", 'data: {"text": "working on it..."}')
    code, msg = sr.decide_exit(st)
    assert code == 1
    assert "mid-run" in msg
    assert not sr.run_completed(st)


def test_render_soft_complete_shows_outcome_not_failure():
    st = sr.SSEState()
    st.response_text = _SUMMARY_BLOCK  # no complete event, but summary present
    out = sr.render_step_summary(st, "1234", "http://run")
    assert "run failed" not in out
    assert "merge-ready (human merges)" in out


def test_malformed_json_does_not_crash():
    st = _stream("event: complete", "data: {not json")
    assert st.completed and st.status == "unknown"


# ---------------------------------------------------------------------------
# summary parsing + mining
# ---------------------------------------------------------------------------

_SUMMARY_BLOCK = (
    "=== SDK RESOLVE SUMMARY ===\n"
    "pr: 1234\n"
    "rounds: 3\n"
    "findings_fixed: 7\n"
    "findings_dismissed: 1\n"
    "ci: green\n"
    "final_verdict: READY_TO_MERGE\n"
    "merge_ready: yes\n"
    "stopped_reason: converged\n"
    "=== END SUMMARY ==="
)


def test_parse_summary_extracts_rows():
    got = sr.parse_summary(_SUMMARY_BLOCK)
    assert got["rounds"] == "3"
    assert got["merge_ready"] == "yes"
    assert got["final_verdict"] == "READY_TO_MERGE"


def test_parse_summary_absent_is_empty():
    assert sr.parse_summary("nothing here") == {}


def test_mine_summary_from_delta_response():
    fragments = [
        line
        for chunk in _SUMMARY_BLOCK.splitlines(keepends=True)
        for line in ("event: response", f'data: {json.dumps({"text": chunk})}')
    ]
    st = _stream(*fragments)
    got = sr.mine_summary(st)
    assert got["findings_fixed"] == "7" and got["merge_ready"] == "yes"


def test_buffers_are_tail_capped_but_keep_the_trailing_summary():
    # Simulate a long stream: lots of chatter, then the summary block at the end.
    lines = []
    for i in range(5000):
        lines.append("event: thought")
        lines.append(f'data: {{"noise": {i}}}')
    for row in _SUMMARY_BLOCK.splitlines():
        lines.append(f"data: {row}")
    st = _stream(*lines)
    assert len(st.raw_data) <= sr.BUFFER_CAP_BYTES
    # The trailing summary survived the tail-cap.
    assert sr.mine_summary(st)["merge_ready"] == "yes"


def test_mine_summary_from_raw_when_not_response_event():
    lines = ["event: thought"]
    for row in _SUMMARY_BLOCK.splitlines():
        lines.append(f"data: {row}")
    st = _stream(*lines)
    assert st.response_text == ""
    assert sr.mine_summary(st)["stopped_reason"] == "converged"


# ---------------------------------------------------------------------------
# step summary rendering
# ---------------------------------------------------------------------------


def test_render_merge_ready():
    st = sr.SSEState()
    st.completed = True
    st.status = "completed"
    st.cost = "9.00"
    st.response_text = _SUMMARY_BLOCK
    out = sr.render_step_summary(st, "1234", "http://run")
    assert "merge-ready (human merges)" in out
    assert "| rounds | 3 |" in out
    assert "READY_TO_MERGE" in out


def test_render_stopped_short():
    # merge_ready: no after real rounds (rounds: 3) → genuine hand-to-human.
    st = sr.SSEState()
    st.completed = True
    st.status = "completed"
    st.cost = "9.00"
    st.response_text = _SUMMARY_BLOCK.replace("merge_ready: yes", "merge_ready: no")
    out = sr.render_step_summary(st, "1234", "http://run")
    assert "stopped short — needs a human" in out
    assert "exited before" not in out  # not the early-exit backstop


def test_render_exited_before_any_round_is_flagged_distinctly():
    # merge_ready: no with rounds: 0 is the "exited before the review returned"
    # bug fingerprint — must render as a distinct, re-runnable outcome, NOT the
    # generic stopped-short (which reads as normal triage and gets ignored).
    st = sr.SSEState()
    st.completed = True
    st.status = "completed"
    st.cost = "3.22"
    st.response_text = _SUMMARY_BLOCK.replace(
        "merge_ready: yes", "merge_ready: no"
    ).replace("rounds: 3", "rounds: 0")
    out = sr.render_step_summary(st, "1234", "http://run")
    assert "exited before completing a review round" in out
    assert "re-run" in out
    assert "stopped short — needs a human" not in out


def test_rounds_completed_parsing():
    assert sr._rounds_completed({"rounds": "3"}) == 3
    assert sr._rounds_completed({"rounds": " 0 "}) == 0
    assert sr._rounds_completed({}) is None
    assert sr._rounds_completed({"rounds": "n/a"}) is None


def test_render_failed_run():
    st = sr.SSEState()
    st.errored = True
    st.err_code = "elicitation"
    st.err_msg = "needs input"
    out = sr.render_step_summary(st, "1234", "")
    assert "run failed" in out and "elicitation" in out


# ---------------------------------------------------------------------------
# out-of-band hand-off backstop (transport false-negative recovery)
# ---------------------------------------------------------------------------

_SINCE = sr._parse_iso8601_epoch("2026-07-17T06:00:00Z")


def test_parse_iso8601_epoch():
    assert sr._parse_iso8601_epoch("2026-07-17T06:34:00Z") == (
        sr._parse_iso8601_epoch("2026-07-17T06:00:00Z") + 34 * 60
    )
    assert sr._parse_iso8601_epoch("not a date") is None
    assert sr._parse_iso8601_epoch("") is None


def _comment(created_at, body, url="http://c"):
    return {"created_at": created_at, "body": body, "html_url": url}


def test_find_oob_summary_matches_marker_after_since():
    comments = [
        _comment(
            "2026-07-17T06:34:00Z",
            f"stuff {sr.RESOLVE_SUMMARY_MARKER} done",
            "http://good",
        ),
    ]
    assert sr.find_oob_summary(comments, _SINCE) == "http://good"


def test_find_oob_summary_ignores_older_than_since():
    # A summary from a PRIOR run (before this run started) must not be matched.
    comments = [
        _comment("2026-07-17T05:00:00Z", sr.RESOLVE_SUMMARY_MARKER, "http://stale")
    ]
    assert sr.find_oob_summary(comments, _SINCE) is None


def test_find_oob_summary_requires_marker():
    comments = [_comment("2026-07-17T06:34:00Z", "a plain human comment", "http://x")]
    assert sr.find_oob_summary(comments, _SINCE) is None


def test_find_oob_summary_picks_newest():
    comments = [
        _comment("2026-07-17T06:10:00Z", sr.RESOLVE_SUMMARY_MARKER, "http://older"),
        _comment("2026-07-17T06:40:00Z", sr.RESOLVE_SUMMARY_MARKER, "http://newer"),
    ]
    assert sr.find_oob_summary(comments, _SINCE) == "http://newer"


def test_oob_poll_budget_by_stream_state():
    # transport drop (events seen, no complete) → long budget
    drop = _stream("event: response", 'data: {"text": "working..."}')
    assert sr.oob_poll_budget(drop) == sr.OOB_POLL_SECONDS_STREAM_DROP
    # sandbox hard error (complete status=error) → short budget
    err = _stream("event: complete", 'data: {"status": "error"}')
    assert sr.oob_poll_budget(err) == sr.OOB_POLL_SECONDS_HARD_ERROR
    # explicit error event → short budget
    erv = _stream("event: error", 'data: {"code": "boom", "message": "x"}')
    assert sr.oob_poll_budget(erv) == sr.OOB_POLL_SECONDS_HARD_ERROR
    # no events at all (VPN/network dead) → do not poll
    assert sr.oob_poll_budget(sr.SSEState()) == 0


def test_poll_returns_url_when_found_first_try():
    found = [_comment("2026-07-17T06:34:00Z", sr.RESOLVE_SUMMARY_MARKER, "http://hit")]
    url = sr.poll_for_oob_summary(
        "42",
        "tok",
        _SINCE,
        100,
        fetch=lambda pr, t: found,
        sleeper=lambda _: None,
        now=lambda: 1000.0,
    )
    assert url == "http://hit"


def test_poll_times_out_returns_none():
    nows = iter([0.0, 0.0, 200.0])  # deadline=100; second check is past it
    url = sr.poll_for_oob_summary(
        "42",
        "tok",
        _SINCE,
        100,
        fetch=lambda pr, t: [],  # never matches
        sleeper=lambda _: None,
        now=lambda: next(nows),
    )
    assert url is None


def test_poll_survives_fetch_errors():
    calls = {"n": 0}

    def flaky_fetch(pr, t):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return [
            _comment("2026-07-17T06:34:00Z", sr.RESOLVE_SUMMARY_MARKER, "http://hit")
        ]

    url = sr.poll_for_oob_summary(
        "42",
        "tok",
        _SINCE,
        100,
        fetch=flaky_fetch,
        sleeper=lambda _: None,
        now=lambda: 0.0,
    )
    assert url == "http://hit" and calls["n"] == 2


def test_poll_noop_without_token_or_budget():
    def sentinel(pr, t):
        raise AssertionError("should not fetch")

    # No token → skip (nothing to authenticate the poll); zero budget → skip.
    assert sr.poll_for_oob_summary("42", "", _SINCE, 100, fetch=sentinel) is None
    assert sr.poll_for_oob_summary("42", "tok", _SINCE, 0, fetch=sentinel) is None


def test_render_step_summary_oob_recovery_shows_success():
    # A dropped stream with no summary in its buffers, recovered via oob_url:
    # renders as success (not "run failed") and links the hand-off comment.
    st = _stream("event: response", 'data: {"text": "working on it..."}')
    assert sr.decide_exit(st)[0] == 1  # would have failed on its own
    out = sr.render_step_summary(st, "1234", "http://run", oob_url="http://handoff")
    assert "run failed" not in out
    assert "completed out-of-band" in out
    assert "http://handoff" in out


# ---------------------------------------------------------------------------
# env preflight + health
# ---------------------------------------------------------------------------


def test_main_missing_required_env_returns_1(monkeypatch):
    for v in ("MOTHERSHIP_URL", "HARNESS_TOKEN", "PR_NUMBER"):
        monkeypatch.delenv(v, raising=False)
    assert sr.main() == 1


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_health_succeeds_first_try():
    assert (
        sr.check_health(
            "http://m", opener=lambda u, timeout=0: _Resp(200), sleeper=lambda _: None
        )
        is True
    )


def test_health_retries_then_fails():
    calls = {"n": 0}

    def opener(url, timeout=0):
        calls["n"] += 1
        return _Resp(503)

    assert sr.check_health("http://m", opener=opener, sleeper=lambda _: None) is False
    assert calls["n"] == sr.HEALTH_RETRIES
