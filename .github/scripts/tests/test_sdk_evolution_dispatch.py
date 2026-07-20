"""Tests for .github/scripts/sdk_evolution_dispatch.py."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sdk_evolution_dispatch as sd


def _stream(*lines: str):
    return sd.process_stream(list(lines))


# ---------------------------------------------------------------------------
# tier resolution
# ---------------------------------------------------------------------------


def test_explicit_tier_is_respected():
    assert sd.resolve_tier("daily", "2026-07-08") == "daily"
    assert sd.resolve_tier("weekly", "2026-07-06") == "weekly"
    assert sd.resolve_tier("WEEKLY", "2026-07-06") == "weekly"


def test_auto_resolves_weekly_on_sunday():
    # 2026-07-05 is a Sunday; 2026-07-08 is a Wednesday.
    assert sd.resolve_tier("auto", "2026-07-05") == "weekly"
    assert sd.resolve_tier("auto", "2026-07-08") == "daily"


def test_auto_with_bad_date_falls_back_to_daily():
    assert sd.resolve_tier("auto", "not-a-date") == "daily"
    assert sd.resolve_tier("", "2026-07-05") == "weekly"  # empty → auto


# ---------------------------------------------------------------------------
# focus / theme resolution
# ---------------------------------------------------------------------------


def test_explicit_focus_is_respected():
    assert sd.resolve_focus("DOCS", "2026-07-06") == "DOCS"
    assert sd.resolve_focus("types+apicompat", "2026-07-06") == "TYPES+APICOMPAT"


def test_auto_focus_rotates_by_weekday():
    # 2026-07-06 Mon, 2026-07-07 Tue, 2026-07-08 Wed, 2026-07-11 Sat.
    assert sd.resolve_focus("auto", "2026-07-06") == "BUG"
    assert sd.resolve_focus("auto", "2026-07-07") == "DOCS"
    assert sd.resolve_focus("auto", "2026-07-08") == "TEST"
    assert sd.resolve_focus("auto", "2026-07-11") == "CONF"


def test_focus_bad_date_or_unknown_falls_back_to_bug():
    assert sd.resolve_focus("auto", "not-a-date") == "BUG"
    assert sd.resolve_focus("NOT-A-FAMILY", "not-a-date") == "BUG"


def test_explicit_theme_is_respected():
    assert sd.resolve_theme("toolkit", "2026-07-05") == "TOOLKIT"


def test_auto_theme_rotates_by_iso_week():
    for date_str in ("2026-07-05", "2026-07-12"):
        y, m, d = (int(x) for x in date_str.split("-"))
        expected = sd.WEEKLY_THEMES[
            dt.date(y, m, d).isocalendar()[1] % len(sd.WEEKLY_THEMES)
        ]
        assert sd.resolve_theme("auto", date_str) == expected
    # Consecutive Sundays advance the rotation by exactly one slot.
    a = sd.WEEKLY_THEMES.index(sd.resolve_theme("auto", "2026-07-05"))
    b = sd.WEEKLY_THEMES.index(sd.resolve_theme("auto", "2026-07-12"))
    assert b == (a + 1) % len(sd.WEEKLY_THEMES)


def test_theme_bad_date_falls_back_to_first():
    assert sd.resolve_theme("auto", "not-a-date") == sd.WEEKLY_THEMES[0]


# ---------------------------------------------------------------------------
# payload / prompt shape
# ---------------------------------------------------------------------------


def test_payload_shape_carries_tier():
    p = sd.build_payload("weekly", "2026-07-05", "http://run", 7, theme="ARCH")
    assert p["mode"] == "direct" and p["stream"] is True
    assert p["source_id"] == "sdk-evolution-weekly-2026-07-05"
    assert p["repositories"] == ["atlanhq/application-sdk"]
    assert p["base_branch"] == "main"
    assert p["metadata"]["tier"] == "weekly"
    assert p["metadata"]["theme"] == "ARCH"
    assert p["metadata"]["consumer_pr_cap"] == 7


def test_payload_base_branch_override():
    # Verification runs point the sandbox at a PR branch's playbook pre-merge.
    p = sd.build_payload("daily", "d", "u", 5, focus="BUG", base_branch="my-branch")
    assert p["base_branch"] == "my-branch"
    assert p["metadata"]["focus"] == "BUG"


def test_prompt_differs_by_tier():
    weekly = sd.build_prompt("weekly", "d", "u", 5, theme="TEMPORAL")
    daily = sd.build_prompt("daily", "d", "u", 5, focus="DOCS")
    assert "WEEKLY run" in weekly and "THEME=TEMPORAL" in weekly
    assert "DAILY run" in daily and "FOCUS=DOCS" in daily
    assert "THEME:" in weekly and "FOCUS:" in daily
    assert "FOCUS:" not in weekly and "THEME:" not in daily
    assert "check-registry.md" in daily  # both tiers point at the registry


def test_consumer_pr_cap_is_consumers_theme_only():
    # The cap only governs the weekly CONSUMERS audit — clutter elsewhere.
    consumers = sd.build_prompt("weekly", "d", "u", 5, theme="CONSUMERS")
    assert "CONSUMER_PR_CAP:  5" in consumers and "CONSUMER_PR_CAP=5" in consumers
    assert "CONSUMER_PR_CAP" not in sd.build_prompt("weekly", "d", "u", 5, theme="ARCH")
    assert "CONSUMER_PR_CAP" not in sd.build_prompt("daily", "d", "u", 5, focus="BUG")


def test_main_missing_required_env_returns_1(monkeypatch):
    monkeypatch.delenv("MOTHERSHIP_URL", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    # Returns 1 before any network call — no KeyError traceback.
    assert sd.main() == 1


# ---------------------------------------------------------------------------
# SSE state machine + exit decision
# ---------------------------------------------------------------------------


def test_successful_complete_stream():
    st = _stream(
        "event: started",
        'data: {"session_id": "s1", "sandbox_id": "b1"}',
        "",
        "event: complete",
        'data: {"status": "completed", "cost_usd": "12.30"}',
    )
    assert st.completed and st.status == "completed" and st.cost == "12.30"
    assert sd.decide_exit(st) == (
        0,
        "SDK Evolution completed successfully (cost=12.30).",
    )


def test_error_event_fails():
    st = _stream("event: error", 'data: {"code": "boom", "message": "kaboom"}')
    code, msg = sd.decide_exit(st)
    assert code == 1 and "boom" in msg


def test_elicitation_treated_as_error():
    st = _stream("event: elicitation", 'data: {"prompt": "need input"}')
    assert st.errored
    assert sd.decide_exit(st)[0] == 1


def test_complete_with_error_status_fails():
    st = _stream(
        "event: complete",
        'data: {"status": "error", "error": {"code": "x", "message": "y"}}',
    )
    code, msg = sd.decide_exit(st)
    assert code == 1 and "x" in msg


def test_no_events_fails():
    assert sd.decide_exit(_stream())[0] == 1


def test_events_but_no_complete_fails():
    st = _stream("event: action", 'data: {"action_name": "grep"}')
    assert sd.decide_exit(st)[0] == 1


def test_malformed_json_data_does_not_crash():
    st = _stream("event: complete", "data: {not json")
    assert st.completed and st.status == "unknown"


# ---------------------------------------------------------------------------
# response text capture + summary parsing
# ---------------------------------------------------------------------------

_SUMMARY_BLOCK = (
    "=== SDK EVOLUTION SUMMARY ===\n"
    "tier: daily\n"
    "run_date: 2026-07-08\n"
    "parent_ticket: https://linear.app/x/BLDX-9\n"
    "discovered: 12\n"
    "killed_prefilter: 3\n"
    "killed_verify: 2\n"
    "killed_feasibility: 1\n"
    "fix_prs: 3\n"
    "design_prs: 1\n"
    "consumer_prs: 0\n"
    "handed_for_review: 4\n"
    "=== END SUMMARY ==="
)


def test_parse_summary_extracts_metrics():
    got = sd.parse_summary(_SUMMARY_BLOCK)
    assert got["discovered"] == "12"
    assert got["fix_prs"] == "3"
    assert got["parent_ticket"] == "https://linear.app/x/BLDX-9"


def test_parse_summary_absent_returns_empty():
    assert sd.parse_summary("no summary here") == {}


def test_parse_summary_tolerates_json_escaped_newlines():
    escaped = _SUMMARY_BLOCK.replace("\n", "\\n")
    got = sd.parse_summary(escaped)
    assert got["handed_for_review"] == "4"


def test_response_event_accumulates_text():
    st = _stream(
        "event: response",
        'data: {"text": "hello"}',
        "",
        "event: response",
        'data: {"content": "world"}',
    )
    assert "hello" in st.response_text and "world" in st.response_text


def test_response_plain_string_payload():
    assert "raw words" in sd._response_text('"raw words"')
    assert sd._response_text('{"nope": 1}') == ""


def test_delta_streamed_summary_reassembles():
    # A response streamed as fragments must not gain separators between them,
    # or a line like "discovered: 12" would split and parse_summary would miss.
    fragments = [
        line
        for chunk in _SUMMARY_BLOCK.splitlines(keepends=True)
        for line in ("event: response", f'data: {json.dumps({"text": chunk})}')
    ]
    st = _stream(*fragments)
    got = sd.mine_summary(st)
    assert got["discovered"] == "12" and got["fix_prs"] == "3"


def test_summary_recovered_from_raw_when_not_a_response_event():
    # Block arrives on some other event as plain data lines (no response text).
    lines = ["event: thought"]
    for row in _SUMMARY_BLOCK.splitlines():
        lines.append(f"data: {row}")
    st = _stream(*lines)
    assert st.response_text == ""  # nothing captured as response
    got = sd.mine_summary(st)
    assert got["handed_for_review"] == "4"


# ---------------------------------------------------------------------------
# step summary rendering
# ---------------------------------------------------------------------------


def test_render_step_summary_with_block():
    st = sd.SSEState()
    st.completed = True
    st.status = "completed"
    st.cost = "9.99"
    st.response_text = _SUMMARY_BLOCK
    out = sd.render_step_summary(st, "daily", "2026-07-08", "http://run")
    assert "✅ completed" in out
    assert "9.99" in out
    assert "| discovered | 12 |" in out
    assert "BLDX-9" in out
    assert "http://run" in out


def test_render_step_summary_without_block_is_still_valid():
    st = sd.SSEState()
    st.completed = True
    st.status = "completed"
    st.cost = "1.00"
    out = sd.render_step_summary(st, "weekly", "2026-07-05", "")
    assert "SDK Evolution — weekly" in out
    assert "No summary block" in out


def test_render_step_summary_failed_run():
    st = sd.SSEState()
    st.errored = True
    st.err_code = "elicitation"
    st.err_msg = "needs input"
    out = sd.render_step_summary(st, "daily", "2026-07-08", "http://run")
    assert "❌ failed" in out
    assert "elicitation" in out


# ---------------------------------------------------------------------------
# health check
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_health_succeeds_first_try():
    def opener(url, timeout=0):
        return _Resp(200)

    assert sd.check_health("http://m", opener=opener, sleeper=lambda _: None) is True


def test_health_retries_then_fails():
    calls = {"n": 0}

    def opener(url, timeout=0):
        calls["n"] += 1
        return _Resp(503)

    assert sd.check_health("http://m", opener=opener, sleeper=lambda _: None) is False
    assert calls["n"] == sd.HEALTH_RETRIES


# ---------------------------------------------------------------------------
# completion-marker backstop (SSE stream drop)
# ---------------------------------------------------------------------------

_MARKER_BODY = f"marker: sdk-evolution-daily-2026-07-08\n\n{_SUMMARY_BLOCK}"


class _ApiResp:
    def __init__(self, obj):
        self._obj = obj

    def read(self):
        return json.dumps(self._obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _api_opener(issues, comments):
    """Fake urlopen routing the two GitHub API calls the poll makes."""

    def opener(req, timeout=0):
        url = req.full_url
        if "/comments" in url:
            return _ApiResp(comments)
        assert "labels=sdk-evolution-marker" in url
        return _ApiResp(issues)

    return opener


def test_fetch_marker_summary_found():
    opener = _api_opener(issues=[{"number": 42}], comments=[{"body": _MARKER_BODY}])
    got = sd.fetch_marker_summary(
        "o/r", "sdk-evolution-daily-2026-07-08", "2026-07-08", "tok", opener
    )
    assert got is not None and got["discovered"] == "12"


def test_fetch_marker_summary_wrong_source_id_is_not_found():
    opener = _api_opener(issues=[{"number": 42}], comments=[{"body": _MARKER_BODY}])
    got = sd.fetch_marker_summary(
        "o/r", "sdk-evolution-weekly-2026-07-05", "2026-07-05", "tok", opener
    )
    assert got is None


def test_fetch_marker_found_but_malformed_block_still_counts():
    # The marker alone proves Stage 7 ran; metrics are best-effort.
    opener = _api_opener(
        issues=[{"number": 42}],
        comments=[{"body": "marker: sdk-evolution-daily-2026-07-08"}],
    )
    got = sd.fetch_marker_summary(
        "o/r", "sdk-evolution-daily-2026-07-08", "2026-07-08", "tok", opener
    )
    assert got == {}


def test_fetch_marker_no_tracking_issue():
    opener = _api_opener(issues=[], comments=[])
    assert sd.fetch_marker_summary("o/r", "sid", "2026-07-08", "tok", opener) is None


def test_fetch_marker_api_error_counts_as_not_found():
    def opener(req, timeout=0):
        raise OSError("boom")

    assert sd.fetch_marker_summary("o/r", "sid", "2026-07-08", "tok", opener) is None


def test_poll_marker_found_on_later_attempt():
    calls = {"n": 0}
    found_opener = _api_opener(
        issues=[{"number": 42}], comments=[{"body": _MARKER_BODY}]
    )

    def opener(req, timeout=0):
        calls["n"] += 1
        if calls["n"] <= 2:  # first attempt: issue exists, no comment yet
            return _api_opener(issues=[{"number": 42}], comments=[])(req, timeout)
        return found_opener(req, timeout)

    got = sd.poll_completion_marker(
        "o/r",
        "sdk-evolution-daily-2026-07-08",
        "2026-07-08",
        "tok",
        opener=opener,
        sleeper=lambda _: None,
        attempts=5,
    )
    assert got is not None and got["fix_prs"] == "3"


def test_poll_marker_times_out():
    opener = _api_opener(issues=[{"number": 42}], comments=[])
    slept = {"n": 0}

    def sleeper(_):
        slept["n"] += 1

    got = sd.poll_completion_marker(
        "o/r", "sid", "2026-07-08", "tok", opener=opener, sleeper=sleeper, attempts=3
    )
    assert got is None
    assert slept["n"] == 2  # no sleep after the final attempt


def test_decide_exit_incomplete_stream_recovered_by_marker():
    st = _stream("event: action", 'data: {"action_name": "grep"}')
    code, msg = sd.decide_exit(st, recovered={"fix_prs": "3"})
    assert code == 0 and "completion marker" in msg
    # An empty recovered dict (marker found, block malformed) still passes.
    assert sd.decide_exit(st, recovered={})[0] == 0
    # No recovery → the original failure.
    assert sd.decide_exit(st, recovered=None)[0] == 1


def test_decide_exit_marker_overrides_spurious_error_event():
    # Production case: the sandbox finished Stage 7 (marker posted), then the
    # stream carried an empty `error` event. Marker is ground truth → success.
    st = _stream("event: error", 'data: {"code": "boom", "message": "kaboom"}')
    code, msg = sd.decide_exit(st, recovered={"fix_prs": "3"})
    assert code == 0 and "completion marker" in msg
    # Without a marker, the error stays an error.
    assert sd.decide_exit(st, recovered=None)[0] == 1


def test_decide_exit_marker_overrides_error_status():
    st = _stream("event: complete", 'data: {"status": "error"}')
    assert sd.decide_exit(st, recovered={})[0] == 0
    assert sd.decide_exit(st, recovered=None)[0] == 1


def test_error_event_with_empty_payload_logs_raw():
    st = sd.SSEState()
    st.event = "error"
    st.got_event = True
    msg = sd.process_line('data: {"detail": "sandbox evicted"}', st)
    assert msg is not None and "raw=" in msg and "sandbox evicted" in msg
    # A well-formed error payload stays concise (no raw dump).
    st2 = sd.SSEState()
    st2.event = "error"
    msg2 = sd.process_line('data: {"code": "boom", "message": "kaboom"}', st2)
    assert msg2 is not None and "raw=" not in msg2


def test_render_step_summary_recovered_run():
    st = sd.SSEState()
    st.got_event = True  # stream started, then dropped
    out = sd.render_step_summary(
        st,
        "daily",
        "2026-07-08",
        "http://run",
        recovered=sd.parse_summary(_SUMMARY_BLOCK),
    )
    assert "recovered via completion marker" in out
    assert "| discovered | 12 |" in out
    assert "BLDX-9" in out
    assert "❌ failed" not in out
