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

# Loading the module put its directory on sys.path, so this is the same
# `_gh_refs` it imported — the transport whose `run` seam the stubs replace.
import _gh_refs  # noqa: E402

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


def _http(status: int, body: str) -> str:
    """A `curl -i` transcript: status line, blank line, body."""
    return f"HTTP/2 {status} whatever\n\n{body}"


def responses(*payloads):
    """Stub the transport with one POLL's answer per entry, cycling on the last.

    Each entry is either a dict (returned as a 200 JSON body) or an int (that
    curl exit code, i.e. a transport failure). An int fails EVERY transport
    attempt of its poll: the shared transport retries a lone blip itself, so a
    poll only sees an unreadable read once its retries are spent.
    """
    queue: list[dict[str, object] | int] = []
    for item in payloads:
        if isinstance(item, int):
            queue.extend([item] * _gh_refs._TRANSPORT_ATTEMPTS)
        else:
            queue.append(item)

    def fake_run(cmd, **kwargs):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, int):
            return subprocess.CompletedProcess(cmd, item, "", "curl: (22) boom")
        return subprocess.CompletedProcess(cmd, 0, _http(200, json.dumps(item)), "")

    return fake_run


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch):
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


def test_returns_success_conclusion_when_run_completes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _gh_refs,
        "run",
        responses({"status": "completed", "conclusion": "success"}),
    )
    assert _poll(FakeClock()) == ("completed", "success")


def test_returns_failure_conclusion_verbatim(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _gh_refs,
        "run",
        responses({"status": "completed", "conclusion": "failure"}),
    )
    assert _poll(FakeClock()) == ("completed", "failure")


def test_keeps_polling_through_pending_statuses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _gh_refs,
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


def test_transient_read_error_does_not_conclude_the_run(
    monkeypatch: pytest.MonkeyPatch,
):
    """The bug this script exists to kill.

    The old shell did `status=$(... | jq -r '.status')` with no error handling, so
    one blip set status=null, fell out of the while loop, and reported
    conclusion=null — failing the SDK job (and ejecting the merge-queue entry)
    while the dispatched run was still running.
    """
    monkeypatch.setattr(
        _gh_refs,
        "run",
        responses(
            22,  # transport failure
            {"status": "in_progress", "conclusion": None},
            {"status": "completed", "conclusion": "success"},
        ),
    )
    assert _poll(FakeClock()) == ("completed", "success")


def test_non_json_response_is_treated_as_a_transient_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run(cmd, **kwargs):
        fake_run.calls += 1
        if fake_run.calls == 1:
            return subprocess.CompletedProcess(
                cmd, 0, _http(200, "<html>proxy</html>"), ""
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            _http(200, json.dumps({"status": "completed", "conclusion": "success"})),
            "",
        )

    fake_run.calls = 0
    monkeypatch.setattr(_gh_refs, "run", fake_run)
    assert _poll(FakeClock()) == ("completed", "success")


def test_an_http_error_is_an_unreadable_poll_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """`--fail` used to turn a 4xx into a curl failure; the shared transport
    returns it as a status instead, and it must still count as one bad read."""
    answers = iter(
        [
            _http(404, json.dumps({"message": "Not Found"})),
            _http(200, json.dumps({"status": "completed", "conclusion": "success"})),
        ]
    )
    monkeypatch.setattr(
        _gh_refs,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, next(answers), ""),
    )
    assert _poll(FakeClock()) == ("completed", "success")
    assert "HTTP 404: Not Found" in capsys.readouterr().out


def test_a_single_transport_blip_is_retried_inside_the_poll(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The shared transport retries a lone curl failure itself, so it costs no
    poll interval and does not count towards the consecutive-error budget."""
    answers = iter(
        [
            subprocess.CompletedProcess([], 28, "", "curl: (28) timed out"),
            subprocess.CompletedProcess(
                [],
                0,
                _http(
                    200, json.dumps({"status": "completed", "conclusion": "success"})
                ),
                "",
            ),
        ]
    )
    monkeypatch.setattr(_gh_refs, "run", lambda cmd, **kw: next(answers))
    clock = FakeClock()
    assert _poll(clock, max_consecutive_errors=1) == ("completed", "success")
    assert clock.now == 60  # one poll interval, not two
    assert "could not read run" not in capsys.readouterr().out


def test_the_token_reaches_curl_on_stdin_never_argv(monkeypatch: pytest.MonkeyPatch):
    """FND-4899: this poll holds the fleet App installation token for up to two
    hours. On argv it is readable by every other process on the runner for the
    life of each request; fed with `-K -` it never leaves the pipe."""
    monkeypatch.setenv("GH_TOKEN", "ghs_secret-installation-token")
    argvs: list[list[str]] = []
    stdins: list[str] = []

    def fake_run(cmd: list[str], **kwargs: str) -> subprocess.CompletedProcess[str]:
        argvs.append(cmd)
        stdins.append(kwargs.get("input", ""))
        return subprocess.CompletedProcess(
            cmd,
            0,
            _http(200, json.dumps({"status": "completed", "conclusion": "success"})),
            "",
        )

    monkeypatch.setattr(_gh_refs, "run", fake_run)
    _poll(FakeClock())

    assert not any("ghs_secret-installation-token" in part for part in argvs[0])
    assert "-K" in argvs[0]
    assert "ghs_secret-installation-token" in stdins[0]
    assert argvs[0][-1] == "https://api.github.com/repos/atlanhq/app/actions/runs/123"


def test_gives_up_after_max_consecutive_errors(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_gh_refs, "run", responses(22))
    status, conclusion = _poll(FakeClock(), max_consecutive_errors=3)
    assert (status, conclusion) == ("unreadable", "unreadable")


def test_error_streak_resets_after_a_good_read(monkeypatch: pytest.MonkeyPatch):
    """Two isolated blips either side of a good read must not add up to a give-up."""
    monkeypatch.setattr(
        _gh_refs,
        "run",
        responses(
            22,
            {"status": "in_progress", "conclusion": None},
            22,
            {"status": "completed", "conclusion": "success"},
        ),
    )
    assert _poll(FakeClock(), max_consecutive_errors=2) == ("completed", "success")


def test_timeout_reports_timeout_not_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _gh_refs,
        "run",
        responses({"status": "in_progress", "conclusion": None}),
    )
    clock = FakeClock()
    assert _poll(clock) == ("timeout", "timeout")
    assert clock.now == 600  # stopped at the budget, did not overrun


def test_timeout_applies_while_reads_are_failing(monkeypatch: pytest.MonkeyPatch):
    """A run whose polls all error must still stop at the budget, not spin forever."""
    monkeypatch.setattr(_gh_refs, "run", responses(22))
    clock = FakeClock()
    status, _ = _poll(clock, max_consecutive_errors=10_000)
    assert status == "timeout"
    assert clock.now == 600


def test_missing_token_is_a_hard_error(monkeypatch: pytest.MonkeyPatch):
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


def test_write_outputs_appends_status_and_conclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    write_outputs("completed", "failure")
    assert output.read_text() == "status=completed\nconclusion=failure\n"


def test_write_outputs_is_a_noop_outside_actions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    write_outputs("completed", "success")  # must not raise


def test_main_exits_zero_even_when_the_dispatched_run_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The caller's dedicated fail-step owns the exit code, so the PR-comment
    steps in between still run."""
    monkeypatch.setattr(
        _gh_refs,
        "run",
        responses({"status": "completed", "conclusion": "failure"}),
    )
    # No local sleep stub: conftest's `_no_retry_sleep` already skips the wait,
    # and its stub ADVANCES `time.monotonic` — which this script's deadline loop
    # reads. Re-stubbing sleep to a plain no-op here would leave that loop
    # spinning against a clock that never moves.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert main(["--repo", "atlanhq/app", "--run-id", "123"]) == 0
    assert "failed with conclusion failure" in capsys.readouterr().out
