"""Tests for .github/scripts/mothership_terminate_session.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import mothership_terminate_session as mts

SESSION = "sdk-review-1234-deadbeef-99-1"


def _requester(status: int, body: str = ""):
    """Build a requester that records the URL it was called with."""
    calls: list[tuple[str, str]] = []

    def request(url: str, token: str) -> tuple[int, str]:
        calls.append((url, token))
        return status, body

    request.calls = calls  # type: ignore[attr-defined]
    return request


def test_builds_destroy_url_with_bearer_token():
    req = _requester(200, "{}")
    mts.terminate("https://mothership.example.dev", "tok", SESSION, req)
    url, token = req.calls[0]  # type: ignore[attr-defined]
    assert url == (
        f"https://mothership.example.dev/api/sandbox/session/{SESSION}?destroy=true"
    )
    assert token == "tok"


def test_trailing_slash_on_base_url_does_not_double_up():
    req = _requester(200)
    mts.terminate("https://mothership.example.dev/", "tok", SESSION, req)
    url, _ = req.calls[0]  # type: ignore[attr-defined]
    assert "//api/sandbox" not in url


@pytest.mark.parametrize("status", [200, 202, 204])
def test_success_statuses_report_termination(status, capsys):
    out = mts.terminate("https://m.example.dev", "tok", SESSION, _requester(status))
    assert "termination requested" in out.lower()
    assert "::warning::" not in capsys.readouterr().out


def test_404_is_a_notice_not_a_warning(capsys):
    """An already-finished session is the expected race, not a problem."""
    out = mts.terminate("https://m.example.dev", "tok", SESSION, _requester(404))
    captured = capsys.readouterr().out
    assert "::notice::" in captured
    assert "::warning::" not in captured
    assert "404" in out


@pytest.mark.parametrize("status", [0, 401, 403, 500, 502])
def test_failure_statuses_warn_that_the_sandbox_may_still_run(status, capsys):
    out = mts.terminate("https://m.example.dev", "tok", SESSION, _requester(status))
    captured = capsys.readouterr().out
    assert "::warning::" in captured
    assert "may keep running" in captured
    assert "failed" in out.lower()


def test_body_preview_is_bounded(capsys):
    req = _requester(500, "x" * 5000)
    mts.terminate("https://m.example.dev", "tok", SESSION, req)
    printed = capsys.readouterr().out
    assert "x" * mts.BODY_PREVIEW_CHARS in printed
    assert "x" * (mts.BODY_PREVIEW_CHARS + 1) not in printed


def test_terminate_never_raises_on_transport_failure():
    """The default requester maps transport errors to status 0, not an exception."""

    def boom(url: str, token: str) -> tuple[int, str]:
        return 0, "VPN down"

    assert mts.terminate("https://m.example.dev", "tok", SESSION, boom)


def test_main_is_a_noop_without_session_id(monkeypatch, capsys):
    monkeypatch.delenv("SESSION_ID", raising=False)
    monkeypatch.setenv("MOTHERSHIP_URL", "https://m.example.dev")
    monkeypatch.setenv("HARNESS_TOKEN", "tok")
    assert mts.main() == 0
    assert "::notice::" in capsys.readouterr().out


def test_main_warns_but_succeeds_without_config(monkeypatch, capsys):
    monkeypatch.setenv("SESSION_ID", SESSION)
    monkeypatch.setenv("MOTHERSHIP_URL", "")
    monkeypatch.setenv("HARNESS_TOKEN", "")
    assert mts.main() == 0
    assert "::warning::" in capsys.readouterr().out


def test_main_always_exits_zero_even_when_termination_fails(monkeypatch, capsys):
    """Teardown must never mask the job's real cancelled/failed outcome."""
    monkeypatch.setenv("SESSION_ID", SESSION)
    monkeypatch.setenv("MOTHERSHIP_URL", "https://m.example.dev")
    monkeypatch.setenv("HARNESS_TOKEN", "tok")
    monkeypatch.setattr(mts, "_default_requester", lambda url, token: (500, "boom"))
    assert mts.main() == 0
    assert "::warning::" in capsys.readouterr().out
