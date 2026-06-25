"""Tests for .github/scripts/mothership_sandbox_dispatch.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import mothership_sandbox_dispatch as md


def _stream(*lines: str):
    return md.process_stream(list(lines))


# ---------------------------------------------------------------------------
# SSE state machine + exit decision
# ---------------------------------------------------------------------------


def test_successful_complete_stream():
    st = _stream(
        "event: started",
        'data: {"session_id": "s1", "sandbox_id": "b1"}',
        "",
        "event: complete",
        'data: {"status": "completed", "cost_usd": "0.42"}',
        "",
    )
    code, msg = md.decide_exit(st)
    assert code == 0
    assert "completed successfully" in msg
    assert st.cost == "0.42"


def test_error_event_fails():
    st = _stream(
        "event: error",
        'data: {"code": "boom", "message": "kaboom"}',
        "",
    )
    code, msg = md.decide_exit(st)
    assert code == 1
    assert "boom" in msg


def test_elicitation_treated_as_error():
    st = _stream("event: elicitation", "data: {}", "")
    assert st.errored is True
    assert md.decide_exit(st)[0] == 1


def test_complete_with_error_status_fails():
    st = _stream(
        "event: complete",
        'data: {"status": "error", "error": {"code": "X", "message": "y"}}',
        "",
    )
    code, _ = md.decide_exit(st)
    assert code == 1
    assert st.status == "error"


def test_no_events_fails():
    st = _stream("", ":", "  ")  # comments / blanks only
    code, msg = md.decide_exit(st)
    assert code == 1
    assert "without a single SSE event" in msg


def test_events_but_no_complete_fails():
    st = _stream("event: action", 'data: {"action_name": "Read"}', "")
    code, msg = md.decide_exit(st)
    assert code == 1
    assert "without a 'complete' event" in msg


def test_event_resets_on_blank_line():
    # A data line after a blank line (no event) is ignored, not misattributed.
    st = _stream("event: started", "", 'data: {"status": "completed"}')
    assert st.completed is False  # the trailing data had no active event


def test_malformed_json_data_does_not_crash():
    st = _stream("event: complete", "data: {not json", "")
    # complete was seen, status defaults to "unknown" -> non-completed -> fail
    assert st.completed is True
    assert md.decide_exit(st)[0] == 1


# ---------------------------------------------------------------------------
# prompt / payload
# ---------------------------------------------------------------------------


def test_payload_shape():
    p = md.build_payload("BLDX-1", "CRITICAL", "2026-06-25", "http://run")
    assert p["mode"] == "direct"
    assert p["repositories"] == ["atlanhq/application-sdk"]
    assert p["source_id"] == "vuln-triage-BLDX-1-2026-06-25"
    assert p["metadata"]["severity"] == "CRITICAL"
    assert "BLDX-1" in p["prompt"] and "CRITICAL" in p["prompt"]


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
    calls = []

    def opener(url, timeout=0):
        calls.append(url)
        return _Resp(200)

    assert md.check_health("http://m", opener=opener, sleeper=lambda _s: None) is True
    assert len(calls) == 1


def test_health_retries_then_fails():
    attempts = []

    def opener(url, timeout=0):
        attempts.append(url)
        return _Resp(503)

    slept = []
    ok = md.check_health("http://m", opener=opener, sleeper=lambda s: slept.append(s))
    assert ok is False
    assert len(attempts) == md.HEALTH_RETRIES
    assert len(slept) == md.HEALTH_RETRIES - 1  # no sleep after the last attempt
