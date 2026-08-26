"""``_poll_workflow_completion``: the last wall-clock deadline in the harness.

Untested before FND-240, which is how it kept a third deadline idiom
(``time.time()``) after the other loops had moved to the monotonic one. The
tests here are about the loop, not the runner: what it returns, when it stops,
and what it does with a status read that failed.

``testing/integration/**`` is omitted from the coverage gate — it runs against a
local Temporal, not in the unit suite — so these are here because the behaviour
is worth pinning, not because a percentage needs them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.integration.runner import BaseIntegrationTest


def _runner(*responses: dict[str, Any]) -> tuple[BaseIntegrationTest, MagicMock]:
    """A runner whose status client answers a scripted sequence.

    Built without ``__init__``: constructing a ``BaseIntegrationTest`` discovers
    credentials and probes a server, and the loop under test needs neither.
    """
    runner = BaseIntegrationTest.__new__(BaseIntegrationTest)
    client = MagicMock()
    script = list(responses)
    client.get_workflow_status.side_effect = lambda *_: script[
        min(client.get_workflow_status.call_count - 1, len(script) - 1)
    ]
    runner.client = client  # type: ignore[attr-defined]
    return runner, client


def _status(status: str) -> dict[str, Any]:
    return {"success": True, "data": {"status": status}}


def test_a_terminal_status_is_returned_on_the_first_poll() -> None:
    runner, client = _runner(_status("COMPLETED"))
    with fake_clock():
        assert (
            runner._poll_workflow_completion("wf-1", "run-1", timeout=60, interval=5)
            == "COMPLETED"
        )
    assert client.get_workflow_status.call_count == 1


def test_running_is_polled_through_until_it_is_not() -> None:
    runner, client = _runner(_status("RUNNING"), _status("RUNNING"), _status("FAILED"))
    with fake_clock() as clock:
        assert (
            runner._poll_workflow_completion("wf-1", "run-1", timeout=60, interval=5)
            == "FAILED"
        )
    assert client.get_workflow_status.call_count == 3
    assert clock.slept == [5, 5]


def test_an_unsuccessful_status_read_is_polled_through() -> None:
    """Kept deliberately uncapped.

    FND-240 notes the missing transient cap; choosing a number for it needs
    evidence about how often a *local* Temporal answers unsuccessfully, and a cap
    guessed here would turn a slow local server into a failed test.
    """
    runner, client = _runner({"success": False, "error": "boom"}, _status("COMPLETED"))
    with fake_clock():
        assert (
            runner._poll_workflow_completion("wf-1", "run-1", timeout=60, interval=5)
            == "COMPLETED"
        )
    assert client.get_workflow_status.call_count == 2


def test_the_budget_stops_the_loop_inside_its_stated_timeout() -> None:
    """The off-by-one the wall-clock version had.

    ``elapsed > timeout`` was checked *after* the probe, so a 30s budget at a 10s
    interval slept a whole interval past its own deadline before noticing. Three
    probes and 20s of sleeping is the corrected shape, and it is the same shape
    every other converted loop in the harness now has.
    """
    runner, client = _runner(_status("RUNNING"))
    with fake_clock() as clock, pytest.raises(TimeoutError, match="did not complete"):
        runner._poll_workflow_completion("wf-1", "run-1", timeout=30, interval=10)
    assert client.get_workflow_status.call_count == 3
    assert clock.slept == [10, 10]


def test_the_timeout_names_the_run_and_what_it_spent() -> None:
    """An operator reading a red leg needs which run, and how long it watched."""
    runner, _ = _runner(_status("RUNNING"))
    with fake_clock(), pytest.raises(TimeoutError) as excinfo:
        runner._poll_workflow_completion("wf-1", "run-1", timeout=30, interval=10)
    message = str(excinfo.value)
    assert "wf-1/run-1" in message
    assert "within 30s" in message
    assert "3 attempt(s)" in message


def test_a_status_read_that_raises_is_not_absorbed() -> None:
    """No transient classifier here, and that is the pre-existing contract: a
    client that raises is a bug in the client, not a blip in the workflow."""
    runner = BaseIntegrationTest.__new__(BaseIntegrationTest)
    client = MagicMock()
    client.get_workflow_status.side_effect = RuntimeError("client is broken")
    runner.client = client  # type: ignore[attr-defined]
    with fake_clock(), pytest.raises(RuntimeError, match="client is broken"):
        runner._poll_workflow_completion("wf-1", "run-1", timeout=60, interval=5)
    assert client.get_workflow_status.call_count == 1
