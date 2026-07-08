"""Tests for .github/scripts/sdk_evolution_dispatch.py."""

from __future__ import annotations

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
# payload / prompt shape
# ---------------------------------------------------------------------------


def test_payload_shape_carries_tier():
    p = sd.build_payload("weekly", "2026-07-05", "http://run", 7)
    assert p["mode"] == "direct" and p["stream"] is True
    assert p["source_id"] == "sdk-evolution-weekly-2026-07-05"
    assert p["repositories"] == ["atlanhq/application-sdk"]
    assert p["metadata"]["tier"] == "weekly"
    assert p["metadata"]["consumer_pr_cap"] == 7


def test_prompt_differs_by_tier():
    weekly = sd.build_prompt("weekly", "d", "u", 5)
    daily = sd.build_prompt("daily", "d", "u", 5)
    assert "WEEKLY run" in weekly and "CONSUMER_PR_CAP=5" in weekly
    assert "DAILY run" in daily
    assert "check-registry.md" in daily  # both tiers point at the registry


def test_consumer_pr_cap_metadata_is_weekly_only():
    # The CONSUMER_PR_CAP metadata row is clutter on daily (daily ignores it).
    assert "CONSUMER_PR_CAP:" in sd.build_prompt("weekly", "d", "u", 5)
    assert "CONSUMER_PR_CAP:" not in sd.build_prompt("daily", "d", "u", 5)


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
