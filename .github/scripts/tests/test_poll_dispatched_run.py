"""Tests for .github/scripts/poll_dispatched_run.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "poll_dispatched_run.py"
_spec = importlib.util.spec_from_file_location("poll_dispatched_run", _MODULE_PATH)
assert _spec and _spec.loader
poll_dispatched_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poll_dispatched_run)

poll = poll_dispatched_run.poll
main = poll_dispatched_run.main
write_outputs = poll_dispatched_run.write_outputs
glyph_for_status = poll_dispatched_run.glyph_for_status


class FakeClock:
    """Monotonic clock advanced only by the sleeps the code under test asks for.

    A real clock would make the timeout assertions depend on wall time; this way
    "did it stop at the budget" is exact and the suite stays instant.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def responses(*payloads):
    """Stub `run` with one curl result per call, cycling on the last payload.

    Each entry is either a dict (returned as a 200 JSON body) or an int
    (returned as that curl exit code, i.e. a transport failure).
    """
    queue = list(payloads)

    def fake_run(cmd, **kwargs):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, int):
            return subprocess.CompletedProcess(cmd, item, "", "curl: (22) boom")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(item), "")

    return fake_run


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "token")


def _poll(clock, **kwargs):
    return poll(
        "atlanhq/app",
        "123",
        interval_seconds=60,
        timeout_seconds=600,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


def test_returns_success_conclusion_when_run_completes(monkeypatch):
    monkeypatch.setattr(
        poll_dispatched_run,
        "run",
        responses({"status": "completed", "conclusion": "success"}),
    )
    assert _poll(FakeClock()) == ("completed", "success")


def test_returns_failure_conclusion_verbatim(monkeypatch):
    monkeypatch.setattr(
        poll_dispatched_run,
        "run",
        responses({"status": "completed", "conclusion": "failure"}),
    )
    assert _poll(FakeClock()) == ("completed", "failure")


def test_keeps_polling_through_pending_statuses(monkeypatch):
    monkeypatch.setattr(
        poll_dispatched_run,
        "run",
        responses(
            {"status": "queued", "conclusion": None},
            {"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "success"},
        ),
    )
    clock = FakeClock()
    assert _poll(clock) == ("completed", "success")
    assert clock.now == 180  # three polls at the 60s cadence


def test_transient_read_error_does_not_conclude_the_run(monkeypatch):
    """The bug this script exists to kill.

    The old shell did `status=$(... | jq -r '.status')` with no error handling, so
    one blip set status=null, fell out of the while loop, and reported
    conclusion=null — failing the SDK job (and ejecting the merge-queue entry)
    while the dispatched run was still running.
    """
    monkeypatch.setattr(
        poll_dispatched_run,
        "run",
        responses(
            22,  # transport failure
            {"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "success"},
        ),
    )
    assert _poll(FakeClock()) == ("completed", "success")


def test_non_json_response_is_treated_as_a_transient_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        fake_run.calls += 1
        if fake_run.calls == 1:
            return subprocess.CompletedProcess(cmd, 0, "<html>502</html>", "")
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"status": "completed", "conclusion": "success"}), ""
        )

    fake_run.calls = 0
    monkeypatch.setattr(poll_dispatched_run, "run", fake_run)
    assert _poll(FakeClock()) == ("completed", "success")


def test_gives_up_after_max_consecutive_errors(monkeypatch):
    monkeypatch.setattr(poll_dispatched_run, "run", responses(22))
    status, conclusion = _poll(FakeClock(), max_consecutive_errors=3)
    assert (status, conclusion) == ("unreadable", "unreadable")


def test_error_streak_resets_after_a_good_read(monkeypatch):
    """Two isolated blips either side of a good read must not add up to a give-up."""
    monkeypatch.setattr(
        poll_dispatched_run,
        "run",
        responses(
            22,
            {"status": "in_progress", "conclusion": None},
            22,
            {"status": "completed", "conclusion": "success"},
        ),
    )
    assert _poll(FakeClock(), max_consecutive_errors=2) == ("completed", "success")


def test_timeout_reports_timeout_not_failure(monkeypatch):
    monkeypatch.setattr(
        poll_dispatched_run,
        "run",
        responses({"status": "in_progress", "conclusion": None}),
    )
    clock = FakeClock()
    assert _poll(clock) == ("timeout", "timeout")
    assert clock.now == 600  # stopped at the budget, did not overrun


def test_timeout_applies_while_reads_are_failing(monkeypatch):
    """A run whose polls all error must still stop at the budget, not spin forever."""
    monkeypatch.setattr(poll_dispatched_run, "run", responses(22))
    clock = FakeClock()
    status, _ = _poll(clock, max_consecutive_errors=10_000)
    assert status == "timeout"
    assert clock.now == 600


def test_missing_token_is_a_hard_error(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="GH_TOKEN"):
        _poll(FakeClock())


def test_glyphs_cover_every_pending_status():
    """A pending status with no glyph would print ❔ and read like an error."""
    for status in poll_dispatched_run.PENDING_STATUSES:
        assert glyph_for_status(status) != "❔"
    assert glyph_for_status("completed") == "✅"
    assert glyph_for_status("something_new") == "❔"


def test_write_outputs_appends_status_and_conclusion(tmp_path, monkeypatch):
    output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    write_outputs("completed", "failure")
    assert output.read_text() == "status=completed\nconclusion=failure\n"


def test_write_outputs_is_a_noop_outside_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    write_outputs("completed", "success")  # must not raise


def test_main_exits_zero_even_when_the_dispatched_run_failed(monkeypatch, capsys):
    """The caller's dedicated fail-step owns the exit code, so the PR-comment
    steps in between still run."""
    monkeypatch.setattr(
        poll_dispatched_run,
        "run",
        responses({"status": "completed", "conclusion": "failure"}),
    )
    monkeypatch.setattr(poll_dispatched_run.time, "sleep", lambda _: None)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert main(["--repo", "atlanhq/app", "--run-id", "123"]) == 0
    assert "failed with conclusion failure" in capsys.readouterr().out
