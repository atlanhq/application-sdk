"""Unit tests for the precondition gate (child I on FND-224).

The property under test throughout is the third answer: a precondition read off a
broken environment must reach the caller as *neither* met *nor* unmet. Every test
here that scripts a failing read asserts on which of the three verdicts came
back, not merely that something went wrong — a gate that collapsed
``INDETERMINATE`` into ``FAILED`` would still pass a test that only checked for a
raise.

The HTTP checks go through a **real** ``httpx.AsyncClient`` over a scripted
``MockTransport`` rather than a patched module global, for the reason the cluster
and Temporal doubles hand back real objects: what is being checked is how the
poll reads a refused connection versus a 503, and a stub that returned whatever
the code asked for could not tell those apart.

Budgets here are deliberately tiny (sub-second) and real. Patching a global clock
in an async test hands the same mock to the event loop's own timer, which
produces flaky ``StopIteration`` failures that read as a bug in the code under
test.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from application_sdk.testing.harness import (
    Budget,
    Expired,
    Indeterminate,
    NeverStarted,
    PreconditionsFailedError,
    PreconditionsIndeterminateError,
    Settled,
    Stalled,
    Verdict,
)
from application_sdk.testing.harness.preconditions import (
    GateReport,
    HealthReading,
    PollerReading,
    PreconditionCheck,
    assert_gate,
    check_no_stale_pollers,
    check_worker_health,
    run_preconditions,
)
from application_sdk.testing.harness.temporal import (
    PollerInfo,
    TaskQueueType,
    TemporalConnectFailedError,
    TemporalReadFailedError,
    WorkflowNotFoundError,
    WorkflowStatus,
)

# A budget small enough that a failing wait finishes inside a unit test, with a
# real monotonic clock.
FAST = Budget(timeout=timedelta(seconds=0.12), poll_interval=timedelta(seconds=0.02))


def _outcome(variant: type, label: str = "check") -> object:
    """Build one outcome of *variant* with the fields that variant requires."""
    common = {"label": label, "attempts": 1, "elapsed": timedelta(seconds=1)}
    if variant is Settled:
        return Settled(value=True, **common)
    if variant is NeverStarted:
        return NeverStarted(grace=timedelta(seconds=1), **common)
    if variant is Stalled:
        return Stalled(
            stall_window=timedelta(seconds=1), fingerprint="frozen", **common
        )
    if variant is Expired:
        return Expired(budget=timedelta(seconds=1), **common)
    return Indeterminate(cause=RuntimeError("tunnel dropped"), **common)


def _check(label: str, outcome: object) -> PreconditionCheck:
    """A check that returns *outcome* without doing any work."""

    async def _run():
        return outcome

    return PreconditionCheck(label=label, run=_run)


# ---------------------------------------------------------------------------
# run_preconditions
# ---------------------------------------------------------------------------


async def test_no_checks_grades_passed() -> None:
    """A suite that declares no preconditions makes no claim, rather than being
    forced to declare one before it can run a single test."""
    report = await run_preconditions(())
    assert report.verdict is Verdict.PASSED
    assert report.outcomes == ()


async def test_every_check_runs_even_after_one_fails() -> None:
    """Accumulation, not short-circuit: one CI run should diagnose every fault in
    an environment, because they arrive in groups."""
    ran: list[str] = []

    def _recording(label: str, outcome: object) -> PreconditionCheck:
        async def _run():
            ran.append(label)
            return outcome

        return PreconditionCheck(label=label, run=_run)

    report = await run_preconditions(
        (
            _recording("first", _outcome(Expired, "first")),
            _recording("second", _outcome(Settled, "second")),
            _recording("third", _outcome(Indeterminate, "third")),
        )
    )

    assert ran == ["first", "second", "third"]
    assert len(report.outcomes) == 3


async def test_a_failed_check_outranks_an_unreadable_one() -> None:
    """A confirmed bad starting state is never softened into "could not tell"."""
    report = await run_preconditions(
        (
            _check("unreadable", _outcome(Indeterminate)),
            _check("failed", _outcome(Expired)),
        )
    )
    assert report.verdict is Verdict.FAILED


async def test_an_unreadable_check_alone_grades_indeterminate() -> None:
    report = await run_preconditions((_check("unreadable", _outcome(Indeterminate)),))
    assert report.verdict is Verdict.INDETERMINATE


async def test_a_check_that_raises_is_recorded_as_indeterminate() -> None:
    """A check whose own code broke has said nothing about the environment — and
    it must not discard the verdicts of the checks that already ran."""

    async def _boom():
        raise RuntimeError("the check itself is broken")

    report = await run_preconditions(
        (
            _check("readable", _outcome(Settled)),
            PreconditionCheck(label="broken", run=_boom),
        )
    )

    assert report.verdict is Verdict.INDETERMINATE
    broken = report.outcomes[1]
    assert isinstance(broken, Indeterminate)
    assert isinstance(broken.cause, RuntimeError)
    assert broken.label == "broken"


async def test_the_report_names_only_the_unmet_checks() -> None:
    report = await run_preconditions(
        (
            _check("healthy", _outcome(Settled, "healthy")),
            _check("wedged", _outcome(Stalled, "wedged")),
        )
    )
    assert [outcome.label for outcome in report.unmet] == ["wedged"]
    assert "wedged" in report.summary()
    assert "healthy" not in report.summary()


def test_an_all_settled_report_summarises_to_empty_string() -> None:
    """So a caller can interpolate the summary without a conditional."""
    report = GateReport(verdict=Verdict.PASSED, outcomes=(_outcome(Settled),))
    assert report.summary() == ""


# ---------------------------------------------------------------------------
# assert_gate
# ---------------------------------------------------------------------------


def test_assert_gate_is_silent_on_a_pass() -> None:
    assert_gate(GateReport(verdict=Verdict.PASSED, outcomes=(_outcome(Settled),)))


def test_assert_gate_raises_a_precondition_leaf_on_failure() -> None:
    report = GateReport(
        verdict=Verdict.FAILED, outcomes=(_outcome(Expired, "worker health"),)
    )
    with pytest.raises(PreconditionsFailedError) as caught:
        assert_gate(report)
    assert caught.value.checks == "worker health"
    assert "starting state" in str(caught.value)


def test_assert_gate_raises_a_dependency_leaf_on_indeterminate() -> None:
    """The two non-passing verdicts must be separable on the type alone: a lane
    that reruns on infrastructure failure acts on the category, not on prose."""
    report = GateReport(
        verdict=Verdict.INDETERMINATE, outcomes=(_outcome(Indeterminate, "pollers"),)
    )
    with pytest.raises(PreconditionsIndeterminateError) as caught:
        assert_gate(report)
    assert caught.value.checks == "pollers"


@pytest.mark.parametrize(
    "verdict, leaf",
    [
        (Verdict.FAILED, PreconditionsFailedError),
        (Verdict.INDETERMINATE, PreconditionsIndeterminateError),
    ],
)
def test_neither_leaf_is_an_assertion_error(verdict: Verdict, leaf: type) -> None:
    """Under pytest that is the difference between an ERROR and a FAILURE — a red
    leg that reads as a regression when the environment was never fit to test."""
    report = GateReport(verdict=verdict, outcomes=(_outcome(Expired),))
    with pytest.raises(leaf) as caught:
        assert_gate(report)
    assert not isinstance(caught.value, AssertionError)


# ---------------------------------------------------------------------------
# check_worker_health
# ---------------------------------------------------------------------------


def _scripted_http(*responses: object) -> httpx.MockTransport:
    """A transport that answers with each item in turn, repeating the last.

    An item is either an ``int`` status or an exception to raise, so one script
    can mix "answered 503" with "refused the connection".
    """
    script = list(responses)

    def _handle(request: httpx.Request) -> httpx.Response:
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, BaseException):
            raise item
        return httpx.Response(int(item), request=request)

    return httpx.MockTransport(_handle)


async def test_worker_health_settles_on_a_2xx() -> None:
    check = check_worker_health(
        "http://localhost:8000/server/health",
        budget=FAST,
        transport=_scripted_http(200),
    )
    outcome = await check.run()
    assert isinstance(outcome, Settled)
    assert outcome.value.healthy


async def test_worker_health_settles_after_a_slow_start() -> None:
    """The normal shape: connection refused while the container boots, then 200."""
    check = check_worker_health(
        "http://localhost:8000/server/health",
        budget=FAST,
        transport=_scripted_http(
            httpx.ConnectError("connection refused"),
            httpx.ConnectError("connection refused"),
            200,
        ),
    )
    outcome = await check.run()
    assert isinstance(outcome, Settled)
    assert outcome.attempts == 3


async def test_a_refused_connection_is_a_reading_not_an_unreadable_probe() -> None:
    """A worker that never comes up is a finding about the thing under test, not
    an unavailable dependency — so this expires, it does not go indeterminate."""
    check = check_worker_health(
        "http://localhost:8000/server/health",
        budget=FAST,
        transport=_scripted_http(httpx.ConnectError("connection refused")),
    )
    outcome = await check.run()
    assert isinstance(outcome, Expired)
    assert outcome.last is not None
    assert outcome.last.status is None
    assert "ConnectError" in outcome.last.error


async def test_a_non_2xx_expires_and_keeps_the_last_status() -> None:
    check = check_worker_health(
        "http://localhost:8000/server/health",
        budget=FAST,
        transport=_scripted_http(503),
    )
    outcome = await check.run()
    assert isinstance(outcome, Expired)
    assert outcome.last == HealthReading(status=503)
    assert str(outcome.last) == "HTTP 503"


async def test_never_answering_is_never_started_when_the_budget_has_a_grace() -> None:
    """Two diagnoses, not one: an endpoint that never answers points at the
    deployment; one that answers 503 forever points at the app."""
    budget = Budget(
        timeout=timedelta(seconds=0.5),
        poll_interval=timedelta(seconds=0.02),
        start_grace=timedelta(seconds=0.04),
    )
    check = check_worker_health(
        "http://localhost:8000/server/health",
        budget=budget,
        transport=_scripted_http(httpx.ConnectError("connection refused")),
    )
    outcome = await check.run()
    assert isinstance(outcome, NeverStarted)


async def test_answering_badly_expires_rather_than_never_starting() -> None:
    budget = Budget(
        timeout=timedelta(seconds=0.12),
        poll_interval=timedelta(seconds=0.02),
        start_grace=timedelta(seconds=0.04),
    )
    check = check_worker_health(
        "http://localhost:8000/server/health",
        budget=budget,
        transport=_scripted_http(500),
    )
    outcome = await check.run()
    assert isinstance(outcome, Expired)


def test_the_health_check_label_names_the_url_by_default() -> None:
    check = check_worker_health("http://localhost:8000/server/health", budget=FAST)
    assert "http://localhost:8000/server/health" in check.label
    assert check_worker_health("http://x/health", budget=FAST, label="mine").label == (
        "mine"
    )


# ---------------------------------------------------------------------------
# check_no_stale_pollers
# ---------------------------------------------------------------------------


def _poller(identity: str, build_id: str | None) -> PollerInfo:
    return PollerInfo(
        identity=identity,
        last_access=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
        task_queue_type=TaskQueueType.WORKFLOW,
        build_id=build_id,
    )


class ScriptedTemporalReader:
    """A reader that answers each ``task_queue_pollers`` call from a script.

    Only the two Protocol methods, so it satisfies ``TemporalReader`` without
    inheriting anything: the same composition property the fixtures exist for.
    """

    def __init__(self, *readings: object) -> None:
        self._script = list(readings)
        self.calls: list[tuple[str, str]] = []

    async def task_queue_pollers(
        self, queue: str, *, namespace: str
    ) -> Sequence[PollerInfo]:
        self.calls.append((queue, namespace))
        item = self._script.pop(0) if len(self._script) > 1 else self._script[0]
        if isinstance(item, BaseException):
            raise item
        return tuple(item)  # type: ignore[arg-type]

    async def workflow_status(
        self, workflow_id: str, *, run_id: str | None = None
    ) -> WorkflowStatus:
        raise NotImplementedError


def _poller_check(reader: object, *, budget: Budget = FAST, build: str | None = "v2"):
    return check_no_stale_pollers(
        reader=reader,  # type: ignore[arg-type]
        queue="atlan-app-e2e",
        namespace="default",
        current_build_id=build,
        budget=budget,
    )


async def test_only_current_pollers_settles() -> None:
    reader = ScriptedTemporalReader([_poller("1@a", "v2"), _poller("2@b", "v2")])
    outcome = await _poller_check(reader).run()
    assert isinstance(outcome, Settled)
    assert outcome.value.stale == ()
    assert reader.calls[0] == ("atlan-app-e2e", "default")


async def test_a_stale_poller_that_drains_settles() -> None:
    """The precondition is allowed to *wait* for the previous scenario's worker
    to go away — that is why it is a bounded poll and not one read."""
    reader = ScriptedTemporalReader(
        [_poller("old@a", "v1"), _poller("new@b", "v2")],
        [_poller("new@b", "v2")],
    )
    outcome = await _poller_check(reader).run()
    assert isinstance(outcome, Settled)


async def test_a_stale_poller_that_never_drains_does_not_settle() -> None:
    reader = ScriptedTemporalReader([_poller("old@a", "v1"), _poller("new@b", "v2")])
    outcome = await _poller_check(reader).run()
    assert not isinstance(outcome, Settled)
    assert outcome.last is not None
    assert [poller.identity for poller in outcome.last.stale] == ["old@a"]


async def test_a_frozen_stale_set_reports_the_stale_identities() -> None:
    """The fingerprint is the one line that says *what* is wrong, so the stale
    worker's identity has to be in it."""
    budget = Budget(
        timeout=timedelta(seconds=0.5),
        poll_interval=timedelta(seconds=0.02),
        stall_timeout=timedelta(seconds=0.05),
    )
    reader = ScriptedTemporalReader([_poller("old@a", "v1"), _poller("new@b", "v2")])
    outcome = await _poller_check(reader, budget=budget).run()
    assert isinstance(outcome, Stalled)
    assert "old@a" in outcome.fingerprint


async def test_an_empty_queue_never_started_rather_than_passing() -> None:
    """Empty is a real answer, and it is the *failing* one: a queue nobody polls
    means the version about to be tested will not pick the work up."""
    budget = Budget(
        timeout=timedelta(seconds=0.5),
        poll_interval=timedelta(seconds=0.02),
        start_grace=timedelta(seconds=0.04),
    )
    outcome = await _poller_check(ScriptedTemporalReader([]), budget=budget).run()
    assert isinstance(outcome, NeverStarted)


async def test_no_versioning_reduces_to_something_is_polling() -> None:
    """``current_build_id=None`` must not report every poller as stale — that
    would red every unversioned deployment."""
    reader = ScriptedTemporalReader([_poller("only@a", None)])
    outcome = await _poller_check(reader, build=None).run()
    assert isinstance(outcome, Settled)


async def test_an_unversioned_poller_is_stale_when_versioning_is_in_use() -> None:
    """The quiet-direction bug ``stale_version_pollers`` exists to prevent: a
    worker with no build id predates versioning, or its config did not take."""
    reader = ScriptedTemporalReader([_poller("ghost@a", None)])
    outcome = await _poller_check(reader).run()
    assert not isinstance(outcome, Settled)


@pytest.mark.parametrize(
    "error",
    [
        TemporalConnectFailedError(message="tunnel dropped"),
        TemporalReadFailedError(message="frontend said no"),
    ],
)
async def test_an_unreadable_frontend_is_indeterminate(error: Exception) -> None:
    """The verdict this whole gate exists to keep available: an expired vcluster
    token is not an empty poller list, and empty is *the* diagnosis here."""
    outcome = await _poller_check(ScriptedTemporalReader(error)).run()
    assert isinstance(outcome, Indeterminate)
    assert isinstance(outcome.cause, type(error))


async def test_a_non_transport_error_propagates() -> None:
    """An error that repeats on every attempt is a bug in the probe: absorbing it
    would spend the whole budget confirming a typo."""
    with pytest.raises(WorkflowNotFoundError):
        await _poller_check(
            ScriptedTemporalReader(WorkflowNotFoundError(message="no such thing"))
        ).run()


def test_the_poller_check_label_names_the_queue_by_default() -> None:
    check = _poller_check(ScriptedTemporalReader([]))
    assert "atlan-app-e2e" in check.label


def test_a_poller_reading_renders_for_a_report_line() -> None:
    assert str(PollerReading()) == "no pollers"
    reading = PollerReading(
        pollers=(_poller("a@1", "v1"), _poller("b@2", "v2")),
        stale=(_poller("a@1", "v1"),),
    )
    assert str(reading) == "2 poller(s), stale: a@1"
