"""Tests for .github/scripts/sdk_resolve_dispatch.py."""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sdk_resolve_dispatch as sr


def _stream(*lines: str):
    return sr.process_stream(list(lines))


def _completed_stream(cost: str = "7.50"):
    """A stream that both ends on the sentinel AND carries the Phase 4 summary.

    Success needs both halves now: the sentinel alone is what a no-op run also
    emits (see `test_complete_without_a_summary_is_a_noop_failure`).
    """
    return _stream(
        "event: response",
        f"data: {json.dumps({'text': _SUMMARY_BLOCK})}",
        "",
        "event: complete",
        f'data: {{"status": "completed", "cost_usd": "{cost}"}}',
    )


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
        model=sr.MAIN_MODEL,
    )
    assert p["mode"] == "direct" and p["stream"] is True
    assert p["source_id"] == "sdk-resolve-1234-2026-07-08"
    assert p["repositories"] == ["atlanhq/application-sdk"]
    assert p["metadata"]["pr_number"] == "1234"
    assert p["metadata"]["max_rounds"] == 8
    assert p["metadata"]["reviewers"] == "reviewer-one,reviewer-two"
    assert p["metadata"]["requester"] == "requester-login"


def test_payload_declares_attributed_gateway_key():
    # Without this, mothership's sandbox API has neither a request-level
    # ai_gateway_key_name nor a snapshot-declared alias to bill against, and
    # fails closed: "No attributed AI Gateway key for this run ... Refusing
    # to fall back to a shared, un-attributed key."
    p = sr.build_payload(
        "1234",
        "http://run",
        8,
        "2026-07-08",
        "reviewer-one,reviewer-two",
        "requester-login",
        model=sr.MAIN_MODEL,
    )
    assert p["ai_gateway_key_name"] == "sdk_review"


def test_payload_pins_all_three_model_lanes():
    # All three lanes must be pinned: leaving any unset silently falls back to
    # mothership's Claude defaults (main -> claude-opus-5, sub-agent ->
    # claude-sonnet-5), and `small_fast_model` unset resolves to `model`.
    p = sr.build_payload(
        "1",
        "u",
        8,
        "2026-07-08",
        "reviewer-one",
        "requester-login",
        model=sr.MAIN_MODEL,
    )
    assert p["model"] == "xai/grok-4.6"
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
    st = _completed_stream("7.50")
    assert st.completed and st.status == "completed" and st.cost == "7.50"
    assert sr.decide_exit(st) == (0, "SDK Resolve completed (cost=7.50).")
    assert sr.resolved_nothing(st) is False


def test_complete_without_a_summary_is_a_noop_failure():
    # Regression (FND-644): two dispatches on PR #3297 ended `status=completed`
    # after 3-5 min having posted no @sdk-review trigger, no report, and pushed
    # nothing — and both runs went green, because the sentinel alone decided the
    # exit code. Proof of work is the Phase 4 summary, so a sentinel without one
    # is a failed run.
    st = _stream(
        "event: started",
        'data: {"session_id": "s1", "sandbox_id": "b1"}',
        "",
        "event: response",
        'data: {"text": "Let me look at the PR."}',
        "",
        "event: complete",
        'data: {"status": "completed", "cost_usd": "1.457863"}',
    )
    assert sr.resolved_nothing(st) is True
    code, msg = sr.decide_exit(st)
    assert code == 1
    assert "no Phase 4 summary" in msg
    assert "nothing pushed" in msg


def test_resolved_nothing_is_false_for_every_other_stream_shape():
    # Only a happy sentinel with no summary is a no-op. A hard error, a
    # transport drop and a real success must each stay off this path, or they
    # would inherit the no-op's short poll and its re-dispatch.
    err = _stream("event: complete", 'data: {"status": "error"}')
    drop = _stream("event: response", 'data: {"text": "working..."}')
    assert sr.resolved_nothing(err) is False
    assert sr.resolved_nothing(drop) is False
    assert sr.resolved_nothing(_completed_stream()) is False


def test_a_summary_does_not_outrank_a_failed_terminal_status():
    # `process_line` leaves `errored` False for a `complete` carrying
    # status=error with an empty error object. Without consulting the terminal
    # status, a run that streamed its Phase 4 summary and *then* died would
    # render as merge-ready while `decide_exit` returned 1 — the exit code and
    # the step summary must never disagree.
    st = _stream(
        "event: response",
        f"data: {json.dumps({'text': _SUMMARY_BLOCK})}",
        "",
        "event: complete",
        'data: {"status": "error"}',
    )
    assert st.errored is False and st.status == "error"
    assert sr.run_completed(st) is False
    assert sr.decide_exit(st)[0] == 1
    out = sr.render_step_summary(st, "1234", "http://run")
    assert "merge-ready" not in out
    assert "run failed" in out
    # Still not a no-op: the sandbox reported a failure, so it keeps the
    # error-code retry path rather than the no-op one.
    assert sr.resolved_nothing(st) is False


def test_noop_run_is_a_stopped_sandbox_with_a_short_poll():
    # The sentinel proves the sandbox stopped, so it will post nothing more:
    # poll briefly for a hand-off that landed just before, then move on. The
    # 45-min stream-drop budget would strand the run for nothing.
    st = _stream("event: complete", 'data: {"status": "completed"}')
    assert sr.sandbox_terminated_abnormally(st) is True
    assert sr.oob_poll_budget(st) == sr.OOB_POLL_SECONDS_HARD_ERROR


def test_noop_run_is_retryable_and_names_itself_in_the_reason():
    # It carries no error code at all, so the code ladder cannot classify it.
    st = _stream("event: complete", 'data: {"status": "completed"}')
    assert st.err_code == ""
    assert sr.is_retryable_fault(st) is True
    _plan = sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)
    ok, reason = _plan.retry, _plan.reason
    assert ok is True
    assert "no-op run" in reason
    assert sr.RETRY_MAIN_MODEL in reason


def test_render_noop_run_is_distinct_from_a_hard_failure():
    st = _stream("event: complete", 'data: {"status": "completed", "cost_usd": "1.45"}')
    out = sr.render_step_summary(st, "3297", "http://run")
    assert "no-op run" in out
    assert "run failed" not in out
    assert "No summary block was emitted" in out


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
        for line in ("event: response", f"data: {json.dumps({'text': chunk})}")
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
        for line in ("event: response", f"data: {json.dumps({'text': chunk})}")
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


# ---------------------------------------------------------------------------
# re-dispatch with a model swap (FND-641)
# ---------------------------------------------------------------------------


def test_retry_payload_swaps_only_the_main_model():
    # The whole point of the retry: a different MODEL. Mothership's own
    # provider fallback already covers a different provider for the same model,
    # and that is what failed. The two fast lanes must NOT move with it —
    # model_routing_env does `fast = small_fast_model or model`.
    # The model is the caller's to name now (FND-764), so the swap this asserts
    # is `attempt_model`'s default ladder — the one a model-fault retry uses.
    first = sr.build_payload(
        "1", "u", 8, "2026-08-19", "rev", "req", attempt=1, model=sr.attempt_model(1)
    )
    retry = sr.build_payload(
        "1", "u", 8, "2026-08-19", "rev", "req", attempt=2, model=sr.attempt_model(2)
    )
    assert first["model"] == sr.MAIN_MODEL
    assert retry["model"] == sr.RETRY_MAIN_MODEL
    assert retry["model"] != first["model"]
    assert retry["small_fast_model"] == first["small_fast_model"] == sr.FAST_MODEL
    assert (
        retry["env_vars"]["CLAUDE_CODE_SUBAGENT_MODEL"]
        == first["env_vars"]["CLAUDE_CODE_SUBAGENT_MODEL"]
        == sr.FAST_MODEL
    )
    # Same prompt (Phase 0 re-reads live PR state, so the retry resumes), but a
    # distinguishable source_id and a recorded attempt number.
    assert retry["prompt"] == first["prompt"]
    assert retry["source_id"] != first["source_id"]
    assert retry["metadata"]["attempt"] == 2


def test_retry_payload_clamps_the_sandbox_timeout():
    # Cancelling the job does not stop the sandbox, so a late retry must not be
    # left billing for the full 2h after the runner dies.
    p = sr.build_payload(
        "1",
        "u",
        8,
        "2026-08-19",
        "rev",
        "req",
        attempt=2,
        model=sr.RETRY_MAIN_MODEL,
        max_timeout_seconds=900,
    )
    assert p["max_timeout_seconds"] == 900
    assert (
        sr.build_payload("1", "u", 8, "d", "rev", "req", model=sr.MAIN_MODEL)[
            "max_timeout_seconds"
        ]
        == sr.STREAM_TIMEOUT_SECONDS
    )


def test_provider_faults_are_retryable():
    for code, msg in (
        ("429", "moonshotai/kimi-k3 is temporarily rate-limited upstream"),
        ("400", "the message at position 21 with role 'assistant' must not be empty"),
        ("sandbox_error", ""),
        ("503", ""),
    ):
        st = sr.SSEState()
        st.errored, st.err_code, st.err_msg = True, code, msg
        assert sr.is_retryable_fault(st) is True, code


def test_provider_text_is_matched_when_the_code_is_flattened():
    # mothership flattens an absent `complete` error code to `none` (and a
    # standalone `error` event defaults it to `unknown`) while still forwarding
    # the provider's text, so code alone is not enough — for THOSE codes.
    for code in ("none", "unknown", ""):
        st = sr.SSEState()
        st.errored, st.err_code = True, code
        st.err_msg = "upstream provider returned an unexpected payload"
        assert sr.is_retryable_fault(st) is True, code


def test_message_patterns_never_override_an_informative_code():
    # Regression: the pattern fallback used to match "<code> <message>" for ANY
    # code, so a permanent fault whose text happened to mention "upstream" was
    # classified retryable and bought a second sandbox that failed identically.
    for code, msg in (
        ("401", "upstream auth failed"),
        ("403", "upstream permission denied"),
        ("422", "prompt is overloaded with tokens"),
    ):
        st = sr.SSEState()
        st.errored, st.err_code, st.err_msg = True, code, msg
        assert sr.is_retryable_fault(st) is False, code


def test_fixed_faults_are_not_retryable():
    for code, msg in (
        ("elicitation", "Sandbox requires interactive input; cannot answer from GHA"),
        ("stream_error", "<urlopen error timed out>"),
        ("401", "bad credentials"),
        ("403", "resource not accessible"),
        ("none", ""),
    ):
        st = sr.SSEState()
        st.errored, st.err_code, st.err_msg = True, code, msg
        assert sr.is_retryable_fault(st) is False, code


def test_retry_decision_fires_on_a_dead_sandbox_with_a_provider_fault():
    st = sr.SSEState()
    st.got_event = st.completed = st.errored = True
    st.status, st.err_code = "error", "429"
    _plan = sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)
    ok, reason = _plan.retry, _plan.reason
    assert ok is True
    assert sr.RETRY_MAIN_MODEL in reason


def test_retry_decision_refuses_a_second_retry():
    st = sr.SSEState()
    st.got_event = st.completed = st.errored = True
    st.status, st.err_code = "error", "429"
    _plan = sr.retry_decision(st, sr.MAX_DISPATCH_ATTEMPTS, 6000, sr.MAIN_MODEL)
    ok, reason = _plan.retry, _plan.reason
    assert ok is False and "attempts" in reason


def test_retry_decision_refuses_a_transport_drop():
    # The sandbox may still be working — a re-dispatch would double-run it.
    st = _stream("event: started", 'data: {"session_id": "s1"}')
    assert sr.sandbox_terminated_abnormally(st) is False
    _plan = sr.retry_decision(st, 1, 6000, sr.MAIN_MODEL)
    ok, reason = _plan.retry, _plan.reason
    assert ok is False and "double-run" in reason


def test_retry_decision_refuses_when_the_job_budget_is_nearly_gone():
    st = sr.SSEState()
    st.got_event = st.completed = st.errored = True
    st.status, st.err_code = "error", "429"
    _plan = sr.retry_decision(st, 1, sr.RETRY_MIN_REMAINING_SECONDS - 1, sr.MAIN_MODEL)
    ok, reason = _plan.retry, _plan.reason
    assert ok is False and "job budget" in reason


def test_attempt_model_swaps_on_the_second_attempt():
    assert sr.attempt_model(1) == sr.MAIN_MODEL
    assert sr.attempt_model(2) == sr.RETRY_MAIN_MODEL


# ---------------------------------------------------------------------------
# dispatch-level HTTP status vs stream-transport error (FND-660 follow-up)
# ---------------------------------------------------------------------------


def _raising_opener(exc):
    def opener(req, timeout=0):
        raise exc

    return opener


def test_an_http_status_on_the_post_is_not_a_stream_error():
    # Regression for run 32347771368: mothership answered the dispatch POST
    # with a 504, and because HTTPError is a URLError subclass it was caught
    # by the stream-error clause and logged "code=stream_error is not a known
    # model/provider fault" — no retry, no out-of-band poll, zero work done.
    exc = urllib.error.HTTPError(
        "http://m/api/sandbox/execute", 504, "Gateway Time-out", {}, None
    )
    st = sr.dispatch_once("http://m", "tok", {}, opener=_raising_opener(exc))
    assert st.err_code == "http_504"
    assert st.errored is True
    _plan = sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)
    ok, reason = _plan.retry, _plan.reason
    assert ok is True
    assert sr.RETRY_MAIN_MODEL in reason


def test_a_permanent_http_status_still_does_not_retry():
    for code, reason_phrase in ((401, "Unauthorized"), (400, "Bad Request")):
        exc = urllib.error.HTTPError(
            "http://m/api/sandbox/execute", code, reason_phrase, {}, None
        )
        st = sr.dispatch_once("http://m", "tok", {}, opener=_raising_opener(exc))
        assert st.err_code == f"http_{code}"
        # The 400 case is the important one: "400" sits in RETRYABLE_ERR_CODES
        # for a provider fault carried inside the stream, and must NOT rescue
        # a dispatch-level 400 — a malformed payload fails identically on any
        # model, so the http_ codespace must stay separate from that one.
        assert sr.is_retryable_fault(st) is False, code


def test_a_genuine_transport_drop_is_still_never_retried():
    # Guards the fix from over-reaching: a plain URLError (not an HTTPError)
    # must still land on the never-retried stream_error path.
    exc = urllib.error.URLError("timed out")
    st = sr.dispatch_once("http://m", "tok", {}, opener=_raising_opener(exc))
    assert st.err_code == "stream_error"
    assert sr.is_retryable_fault(st) is False


def test_the_http_error_body_reaches_the_error_message():
    body = b"Invalid model name passed in model=xai/grok-4.6" + b"x" * 600

    class _FakeFp:
        def read(self):
            return body

        def close(self):
            pass

    exc = urllib.error.HTTPError(
        "http://m/api/sandbox/execute", 400, "Bad Request", {}, _FakeFp()
    )
    st = sr.dispatch_once("http://m", "tok", {}, opener=_raising_opener(exc))
    assert "Invalid model name" in st.err_msg
    assert len(st.err_msg) < len(body.decode()) + 100  # truncated, not dumped whole

    # e.read() itself raising must not escape dispatch_once.
    class _BrokenFp:
        def read(self):
            raise OSError("already consumed")

        def close(self):
            pass

    exc2 = urllib.error.HTTPError(
        "http://m/api/sandbox/execute", 502, "Bad Gateway", {}, _BrokenFp()
    )
    st2 = sr.dispatch_once("http://m", "tok", {}, opener=_raising_opener(exc2))
    assert st2.err_code == "http_502"


def test_main_model_is_not_an_openrouter_style_id():
    # Weak guard, deliberately: CI has no LiteLLM key, so the real check
    # (GET /v1/models on llmproxy.atlan.dev) cannot run here. This only catches
    # the specific `x-ai/` vs `xai/` prefix confusion that broke FND-660 —
    # `x-ai/grok-4.6` is the OpenRouter-style id and this proxy rejects it.
    assert "x-ai/" not in sr.MAIN_MODEL
    assert "x-ai/" not in sr.FAST_MODEL


def test_total_cost_sums_attempts_and_skips_unreported_ones():
    a = sr.SSEState()
    a.cost = "1.75"
    b = sr.SSEState()
    b.cost = ""
    c = sr.SSEState()
    c.cost = "0.25"
    assert sr.total_cost([sr.Attempt(1, "m", a), sr.Attempt(2, "n", c)]) == "2"
    assert sr.total_cost([sr.Attempt(1, "m", a), sr.Attempt(2, "n", b)]) == "1.75"
    assert sr.total_cost([sr.Attempt(1, "m", b)]) == ""


def test_attempt_trail_renders_both_attempts_costs_separately():
    first = sr.SSEState()
    first.completed = first.errored = True
    first.status, first.cost, first.err_code = "error", "1.75", "429"
    first.err_msg = "rate-limited\nupstream | pipe"
    second = _stream(
        "event: complete", 'data: {"status": "completed", "cost_usd": "2.10"}'
    )
    attempts = [
        sr.Attempt(1, sr.MAIN_MODEL, first),
        sr.Attempt(2, sr.RETRY_MAIN_MODEL, second),
    ]
    out = sr.render_step_summary(second, "1234", "http://run", None, attempts)
    assert "### Attempts" in out
    assert "1.75" in out and "2.10" in out
    assert f"`{sr.MAIN_MODEL}`" in out and f"`{sr.RETRY_MAIN_MODEL}`" in out
    # Headline cost is the sum, and says so, so the retry's spend is not hidden.
    assert "**Cost:** 3.85 USD across 2 attempts" in out
    # Multi-line / pipe-bearing error text must not break the table.
    assert "rate-limited upstream \\| pipe" in out


def test_attempt_trail_flags_an_attempt_that_reported_no_cost():
    first = sr.SSEState()
    first.completed = first.errored = True
    first.status, first.cost, first.err_code = "error", "", "429"
    second = _stream(
        "event: complete", 'data: {"status": "completed", "cost_usd": "2.10"}'
    )
    out = sr.render_step_summary(
        second,
        "1234",
        "http://run",
        None,
        [
            sr.Attempt(1, sr.MAIN_MODEL, first),
            sr.Attempt(2, sr.RETRY_MAIN_MODEL, second),
        ],
    )
    assert "lower bound" in out


def test_single_attempt_renders_no_attempt_trail():
    st = _stream("event: complete", 'data: {"status": "completed", "cost_usd": "7.50"}')
    out = sr.render_step_summary(
        st, "1234", "http://run", None, [sr.Attempt(1, "m", st)]
    )
    assert "### Attempts" not in out
    assert "**Cost:** 7.50 USD  " in out


# --- main() end-to-end over the retry loop ---------------------------------


def _main_env(monkeypatch):
    for k, v in {
        "MOTHERSHIP_URL": "http://m",
        "HARNESS_TOKEN": "t",
        "PR_NUMBER": "1234",
        "GITHUB_TOKEN": "gh",
        "MAX_ROUNDS": "8",
        "RUN_DATE": "2026-08-19",
        "GHA_RUN_URL": "http://run",
        "REVIEWERS": "rev",
        "REQUESTER": "req",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(sr, "check_health", lambda *a, **k: True)
    # No out-of-band hand-off landed: the sandbox really is dead.
    monkeypatch.setattr(sr, "poll_for_oob_summary", lambda *a, **k: None)


def _record_dispatches(monkeypatch, states):
    """Stub dispatch_once to return `states` in order; capture each payload."""
    seen = []

    def fake(base_url, token, payload, opener=None):
        seen.append(payload)
        return states[len(seen) - 1]

    monkeypatch.setattr(sr, "dispatch_once", fake)
    return seen


def test_main_redispatches_on_a_model_fault_and_recovers(monkeypatch, capsys):
    _main_env(monkeypatch)
    dead = sr.SSEState()
    dead.got_event = dead.completed = dead.errored = True
    dead.status, dead.cost, dead.err_code = "error", "1.75", "429"
    dead.err_msg = "moonshotai/kimi-k3 is temporarily rate-limited upstream"
    good = _completed_stream("2.10")
    payloads = _record_dispatches(monkeypatch, [dead, good])

    assert sr.main() == 0
    assert [p["model"] for p in payloads] == [sr.MAIN_MODEL, sr.RETRY_MAIN_MODEL]
    # The background lanes stay put across the swap.
    assert {p["small_fast_model"] for p in payloads} == {sr.FAST_MODEL}
    out = capsys.readouterr().out
    assert "Re-dispatching on a fresh sandbox" in out
    # Both attempts' costs are logged separately.
    assert "cost_usd=1.75" in out and "cost_usd=2.10" in out


def test_main_redispatches_a_noop_run_on_the_other_model(monkeypatch, capsys):
    # End-to-end of the FND-644 path: attempt 1 reports success having done
    # nothing, so the run must NOT go green on it — it re-dispatches on the
    # other main model and succeeds there.
    _main_env(monkeypatch)
    noop = _stream(
        "event: complete", 'data: {"status": "completed", "cost_usd": "1.45"}'
    )
    payloads = _record_dispatches(monkeypatch, [noop, _completed_stream("2.10")])

    assert sr.main() == 0
    assert [p["model"] for p in payloads] == [sr.MAIN_MODEL, sr.RETRY_MAIN_MODEL]
    out = capsys.readouterr().out
    assert "no-op run" in out
    # The no-op attempt's spend stays visible rather than hiding behind the
    # recovered attempt's success.
    assert "cost_usd=1.45" in out


def test_main_fails_when_both_attempts_are_noop_runs(monkeypatch, capsys):
    # No silent green when the swap does not help either.
    _main_env(monkeypatch)
    noop = _stream("event: complete", 'data: {"status": "completed"}')
    payloads = _record_dispatches(monkeypatch, [noop, noop])

    assert sr.main() == 1
    assert len(payloads) == 2
    assert "both attempts failed" in capsys.readouterr().out


def test_main_does_not_redispatch_an_elicitation(monkeypatch, capsys):
    _main_env(monkeypatch)
    st = _stream("event: elicitation", "data: {}")
    payloads = _record_dispatches(monkeypatch, [st])

    assert sr.main() == 1
    assert len(payloads) == 1
    assert "Not re-dispatching" in capsys.readouterr().out


def test_main_does_not_redispatch_a_transport_drop(monkeypatch, capsys):
    # Stream cut mid-work: the resolver is probably still running. Retrying here
    # would double-run it, so this stays fail-fast even though it exits 1.
    _main_env(monkeypatch)
    st = _stream(
        "event: started",
        'data: {"session_id": "s1"}',
        "",
        "event: response",
        'data: {"text": "working"}',
    )
    payloads = _record_dispatches(monkeypatch, [st])

    assert sr.main() == 1
    assert len(payloads) == 1
    assert "double-run" in capsys.readouterr().out


def test_main_stops_after_one_retry(monkeypatch, capsys):
    _main_env(monkeypatch)

    def dead():
        st = sr.SSEState()
        st.got_event = st.completed = st.errored = True
        st.status, st.cost, st.err_code = "error", "1.75", "429"
        return st

    payloads = _record_dispatches(monkeypatch, [dead(), dead()])

    assert sr.main() == 1
    assert len(payloads) == sr.MAX_DISPATCH_ATTEMPTS
    out = capsys.readouterr().out
    assert "already used all" in out
    assert "both attempts failed" in out


def test_main_prefers_an_out_of_band_handoff_over_a_retry(monkeypatch):
    # The dead sandbox had already posted its Phase-4 hand-off — the run is
    # done, so spending a second sandbox on it would be pure waste.
    _main_env(monkeypatch)
    monkeypatch.setattr(sr, "poll_for_oob_summary", lambda *a, **k: "http://handoff")
    dead = sr.SSEState()
    dead.got_event = dead.completed = dead.errored = True
    dead.status, dead.err_code = "error", "429"
    payloads = _record_dispatches(monkeypatch, [dead])

    assert sr.main() == 0
    assert len(payloads) == 1


def test_main_writes_the_attempt_trail_to_the_step_summary(monkeypatch, tmp_path):
    _main_env(monkeypatch)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    dead = sr.SSEState()
    dead.got_event = dead.completed = dead.errored = True
    dead.status, dead.cost, dead.err_code = "error", "1.75", "429"
    good = _completed_stream("2.10")
    _record_dispatches(monkeypatch, [dead, good])

    assert sr.main() == 0
    text = summary.read_text()
    assert "### Attempts" in text
    assert "**Cost:** 3.85 USD across 2 attempts" in text


def test_main_skips_the_oob_poll_and_the_retry_when_the_budget_is_spent(
    monkeypatch, capsys
):
    # With no job budget left there is time for neither a 120s hand-off poll nor
    # a second sandbox — the runner's 130-min timeout would kill the job before
    # either finished, and take the step summary with it.
    _main_env(monkeypatch)
    monkeypatch.setattr(sr, "DISPATCH_BUDGET_SECONDS", 0)
    polled = []
    monkeypatch.setattr(
        sr, "poll_for_oob_summary", lambda *a, **k: polled.append(a) or None
    )
    dead = sr.SSEState()
    dead.got_event = dead.completed = dead.errored = True
    dead.status, dead.err_code = "error", "429"
    payloads = _record_dispatches(monkeypatch, [dead])

    assert sr.main() == 1
    assert polled == []
    assert len(payloads) == 1
    assert "job budget" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# a dropped RPC to the sandbox: long poll, never a re-dispatch (FND-647)
# ---------------------------------------------------------------------------

# Verbatim from run 32309963717 (PR #3276): the code is flattened to `unknown`
# and the provider text arrives as a nested JSON blob, so the pattern has to
# match inside it.
_RPC_DROP = (
    '{"type":"error","message":"ReadableStream received over RPC '
    'disconnected prematurely."}'
)


def _error_event(code: str, message: str) -> list[str]:
    return ["event: error", f"data: {json.dumps({'code': code, 'message': message})}"]


def _rpc_dropped():
    """That run's ending: a clean `complete`, then a dropped-RPC error event."""
    return _stream(
        "event: complete",
        'data: {"status": "completed", "cost_usd": "0.686704"}',
        "",
        *_error_event("unknown", _RPC_DROP),
    )


def test_a_dropped_rpc_outranks_the_noop_classification():
    """The live hazard this closes, and why the branch is ranked where it is.

    An error event leaves `run_completed` False, so a clean `complete` plus a
    dropped RPC is indistinguishable from a sandbox that stopped having done
    nothing — and FND-644's no-op branch answers `is_retryable_fault` True on
    that basis. Left alone it would spend a second resolver against a first one
    of UNKNOWN liveness, which is the double-push this lane cannot undo. (The
    run that motivated FND-647 predates that branch by a day, so its log shows
    the older, opposite failure: no retry at all.)
    """
    st = _rpc_dropped()
    assert sr.is_transport_fault(st) is True
    assert sr.resolved_nothing(st) is True
    assert sr.is_retryable_fault(st) is True  # would have re-dispatched
    # `errored` also makes it look dead, which is what bought it the 120s poll.
    assert sr.sandbox_terminated_abnormally(st) is True
    assert (
        sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)[0] is False
    )


def test_a_dropped_rpc_gets_the_long_out_of_band_poll():
    """The pipe dropped, not the resolver — it may be minutes from Phase 4."""
    assert sr.oob_poll_budget(_rpc_dropped()) == sr.OOB_POLL_SECONDS_STREAM_DROP
    assert sr.OOB_POLL_SECONDS_STREAM_DROP > sr.OOB_POLL_SECONDS_HARD_ERROR


def test_a_dropped_rpc_is_never_re_dispatched_on_this_lane():
    """A resolver pushes commits; two on one branch cannot be undone."""
    _plan = sr.retry_decision(
        _rpc_dropped(), 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL
    )
    ok, reason = _plan.retry, _plan.reason
    assert ok is False
    assert "dropped RPC" in reason and "cannot be undone" in reason


def test_a_known_code_is_never_overridden_by_the_transport_patterns():
    """A code that carries information decides on its own, as everywhere else."""
    st = _stream(*_error_event("401", _RPC_DROP))
    assert sr.is_transport_fault(st) is False
    assert (
        sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)[0] is False
    )


def test_a_dispatch_level_http_status_is_never_the_transport_class():
    """No sandbox was ever started, so no RPC to one can have dropped."""
    st = sr.SSEState()
    st.got_event = st.errored = True
    st.err_code, st.err_msg = "http_504", f"HTTP 504 on dispatch POST: {_RPC_DROP}"
    assert sr.is_transport_fault(st) is False
    # And it keeps the model swap the http_ codespace already earned it.
    assert (
        sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)[0] is True
    )


def test_a_model_fault_still_swaps_the_model():
    """No regression on FND-641: the model-swap class keeps its own axis."""
    st = sr.SSEState()
    st.got_event = st.completed = st.errored = True
    st.status, st.err_code = "error", "429"
    st.err_msg = "temporarily rate-limited upstream"
    assert sr.is_transport_fault(st) is False
    _plan = sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)
    ok, reason = _plan.retry, _plan.reason
    assert ok is True and sr.RETRY_MAIN_MODEL in reason


def test_main_waits_out_a_dropped_rpc_instead_of_re_dispatching(monkeypatch, capsys):
    """End-to-end: the long poll finds the hand-off and the run is recovered."""
    _main_env(monkeypatch)
    budgets = []

    def fake_poll(pr, token, since, budget, **_k):
        budgets.append(budget)
        return "http://handoff"

    monkeypatch.setattr(sr, "poll_for_oob_summary", fake_poll)
    payloads = _record_dispatches(monkeypatch, [_rpc_dropped()])

    assert sr.main() == 0
    assert len(payloads) == 1  # never a second resolver on the branch
    assert budgets == [sr.OOB_POLL_SECONDS_STREAM_DROP]
    assert "out-of-band" in capsys.readouterr().out


def test_main_fails_a_dropped_rpc_whose_handoff_never_lands(monkeypatch, capsys):
    """Nothing to show for it, but still no second resolver: the run is spent."""
    _main_env(monkeypatch)
    payloads = _record_dispatches(monkeypatch, [_rpc_dropped()])

    assert sr.main() == 1
    assert len(payloads) == 1
    assert "Not re-dispatching" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# a fault in mothership's own sandbox-api wrapper: same-model retry (FND-764)
# ---------------------------------------------------------------------------

# Verbatim from the 9 consecutive resolve failures on 2026-08-24 (runs
# 32712863922 .. 32739465231). mothership's `langfuse_lifecycle_span` swallowed
# the real exception and yielded a second time, so `contextlib` raised this in
# its place and the actual cause never reached us (FND-765).
_SANDBOX_API_MASKED = "[sandbox-api/_execute_sync] generator didn't stop after throw()"
# The other observed shape, from run 32494993256 — legible, and plainly
# transient. A different cause from the one above; the same retryability.
_SANDBOX_API_CLONE = (
    "[sandbox-api/_execute_sync] Failed to clone repository: "
    '{"error":"CommandError: ... Command timeout after 60000ms"}'
)


def _sandbox_api_dead(message: str = _SANDBOX_API_MASKED):
    """How those runs ended: `complete` with status=error, no cost, then the code.

    `cost_usd` is empty on purpose — it is empty on every observed occurrence,
    which is the evidence that no model produced a token.
    """
    return _stream(
        "event: complete",
        'data: {"status": "error", "cost_usd": ""}',
        "",
        *_error_event("internal", message),
    )


def test_a_sandbox_api_fault_is_recognised_by_code_and_prefix():
    for msg in (_SANDBOX_API_MASKED, _SANDBOX_API_CLONE):
        assert sr.is_sandbox_api_fault(_sandbox_api_dead(msg)) is True


def test_a_sandbox_api_fault_retries_on_the_SAME_model():
    """The whole point of the class: the model never failed, so don't swap it.

    Before FND-764 this fell out of `is_retryable_fault` as "not a known
    model/provider fault" — true, and beside the point.
    """
    st = _sandbox_api_dead()
    assert sr.is_transport_fault(st) is False
    plan = sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)
    assert plan.retry is True
    assert plan.model == sr.MAIN_MODEL != sr.RETRY_MAIN_MODEL
    assert "sandbox-api" in plan.reason


def test_an_internal_code_without_the_prefix_still_refuses_to_retry():
    """The guard that keeps this an allowlist rather than a denylist.

    `internal` is a generic label. A future non-plumbing use of it must not
    inherit a retry that was only ever argued for the sandbox-api codespace.
    """
    st = _sandbox_api_dead("upstream model rejected the request")
    assert sr.is_sandbox_api_fault(st) is False
    plan = sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)
    assert plan.retry is False
    assert plan.model == ""
    assert "not a known model/provider fault" in plan.reason


def test_the_budget_floor_still_bounds_the_sandbox_api_class():
    plan = sr.retry_decision(
        _sandbox_api_dead(), 1, sr.RETRY_MIN_REMAINING_SECONDS - 1, sr.MAIN_MODEL
    )
    assert plan.retry is False
    assert "job budget" in plan.reason


def test_the_attempt_cap_still_bounds_the_sandbox_api_class():
    plan = sr.retry_decision(
        _sandbox_api_dead(),
        sr.MAX_DISPATCH_ATTEMPTS,
        sr.DISPATCH_BUDGET_SECONDS,
        sr.MAIN_MODEL,
    )
    assert plan.retry is False


def test_main_redispatches_a_sandbox_api_fault_on_the_same_model(monkeypatch, capsys):
    """End-to-end: two dispatches, both on MAIN_MODEL, and the run recovers."""
    _main_env(monkeypatch)
    payloads = _record_dispatches(
        monkeypatch, [_sandbox_api_dead(), _completed_stream("2.10")]
    )

    assert sr.main() == 0
    assert [p["model"] for p in payloads] == [sr.MAIN_MODEL, sr.MAIN_MODEL]
    # A retry MUST NOT collide with attempt 1's source_id on the mothership side.
    assert payloads[0]["source_id"] != payloads[1]["source_id"]
    assert "Re-dispatching" in capsys.readouterr().out


def test_main_names_both_models_when_a_same_model_retry_also_fails(monkeypatch, capsys):
    """The failure suffix must report what actually ran, not attempt_model().

    With the model no longer a function of the attempt number, deriving the
    suffix from `attempt_model(...)` would claim a swap to RETRY_MAIN_MODEL that
    never happened.
    """
    _main_env(monkeypatch)
    payloads = _record_dispatches(
        monkeypatch, [_sandbox_api_dead(), _sandbox_api_dead()]
    )

    assert sr.main() == 1
    assert len(payloads) == 2
    out = capsys.readouterr().out
    assert f"retried on {sr.MAIN_MODEL} after {sr.MAIN_MODEL} failed" in out


def test_the_clone_timeout_variant_keeps_its_cause_in_the_reason():
    """The one shape of this class whose message is worth reading.

    Its cause sits at the very end, behind a JSON envelope, so the 80-char cap
    the other classes use would truncate it away in the line a human triages
    from.
    """
    plan = sr.retry_decision(
        _sandbox_api_dead(_SANDBOX_API_CLONE),
        1,
        sr.DISPATCH_BUDGET_SECONDS,
        sr.MAIN_MODEL,
    )
    assert plan.retry is True
    assert "Command timeout after 60000ms" in plan.reason


def _sandbox_api_billed(cost: str, message: str = _SANDBOX_API_MASKED):
    """The same fault, but with real spend reported — so something DID run."""
    return _stream(
        "event: complete",
        f'data: {{"status": "error", "cost_usd": "{cost}"}}',
        "",
        *_error_event("internal", message),
    )


def test_a_sandbox_api_fault_that_billed_is_never_retried():
    """The conjunct that makes this class safe on a lane that pushes commits.

    The empty `cost_usd` is the whole safety argument — it is what establishes
    that no model produced a token and therefore that no resolver can have
    pushed. Real spend means the wrapper faulted AFTER a run started, and a
    second resolver on one branch cannot be undone.
    """
    st = _sandbox_api_billed("12.34")
    assert sr.billed_nothing(st) is False
    assert sr.is_sandbox_api_fault(st) is False
    plan = sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL)
    assert plan.retry is False
    assert plan.model == ""
    assert "not a known model/provider fault" in plan.reason


def test_a_zero_cost_still_counts_as_nothing_billed():
    for cost in ("", "0", "0.0", "0.0000"):
        assert sr.billed_nothing(_sandbox_api_billed(cost)) is True
        assert sr.is_sandbox_api_fault(_sandbox_api_billed(cost)) is True


def test_an_unreadable_cost_fails_open_to_the_pre_guard_behaviour():
    """`total_cost` documents mothership's cost telemetry as unreliable.

    That unreliability is false-EMPTY, so an unparseable value must not be read
    as evidence of spend — it can only ever return this to the behaviour that
    shipped before the guard, never to something stricter than the evidence.
    """
    st = _sandbox_api_billed("n/a")
    assert sr.billed_nothing(st) is True
    assert sr.is_sandbox_api_fault(st) is True


def test_a_sandbox_api_fault_is_matched_anywhere_in_the_message():
    """mothership does not deliver the message bare consistently.

    The transport class on this lane exists because the provider text arrived
    wrapped in a nested JSON blob (see `_RPC_DROP`), so anchoring this class to
    a prefix would silently miss the same fault in the same envelope — failing
    closed, and failing to do the one job the class was added for.
    """
    wrapped = (
        '{"type":"error","message":"[sandbox-api/_execute_sync] generator '
        "didn't stop after throw()\"}"
    )
    st = _sandbox_api_dead(wrapped)
    assert sr.is_sandbox_api_fault(st) is True
    assert sr.retry_decision(st, 1, sr.DISPATCH_BUDGET_SECONDS, sr.MAIN_MODEL).retry


def test_the_same_model_is_the_one_that_RAN_not_the_one_attempt_n_implies():
    """The invariant the RetryPlan port exists to hold.

    `attempt_model(attempt)` answers "what attempt N would have run by default",
    which stops being the same answer the moment a same-model retry has
    happened. Passing a model that is neither MAIN_MODEL nor RETRY_MAIN_MODEL
    proves the plan reports what actually ran rather than re-deriving it.
    """
    ran = "some/other-model"
    plan = sr.retry_decision(_sandbox_api_dead(), 1, sr.DISPATCH_BUDGET_SECONDS, ran)
    assert plan.retry is True
    assert plan.model == ran
    assert f"same model ({ran})" in plan.reason


def test_build_payload_requires_the_model_rather_than_deriving_it():
    """No `or attempt_model(attempt)` fallback: forgetting it must be an error.

    A silent fallback would put a same-model retry back on RETRY_MAIN_MODEL —
    the exact bug the explicit `model` argument removes.
    """
    with pytest.raises(TypeError):
        sr.build_payload("1", "u", 8, "d", "rev", "req", attempt=2)  # type: ignore[call-arg]
