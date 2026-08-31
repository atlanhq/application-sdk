"""The precondition gate: what has to be true before a scenario dispatches work.

Every scenario asserts its starting state before it does anything, because that
is what splits one ambiguous failure into two distinct ones — *a bad starting
state* versus *a real bug* — and because passing preconditions are the only
available proof that the previous scenario cleaned up after itself.

Both consumer sides arrived at the same requirement from opposite directions:

* the connector suites have ``BaseE2ETest.assert_worker_up`` — the CI worker
  container answers ``/server/health`` before the DAG is submitted;
* the runtime scenario suite requires "the intended version is Current, **and no
  stale-version workers are still polling**" before every scenario.

Those are the same check in two substrates, so they are two builders over one
gate rather than two gates. :func:`check_worker_health` and
:func:`check_no_stale_pollers` each return a :class:`PreconditionCheck`;
:func:`run_preconditions` runs a list of them and grades the result.

**The gate may return "could not tell".** That is the whole reason it is a gate
and not a pile of ``assert`` statements. A precondition read off a broken
environment — an expired vcluster token, a Temporal frontend that will not answer
— is neither "the starting state is good" nor "the starting state is bad", and
reporting it as either is worse than reporting it as neither. So a check returns
an :class:`~application_sdk.testing.harness.outcome.Outcome` rather than a bool,
:func:`~application_sdk.testing.harness.outcome.grade` reduces the pile to one
:class:`~application_sdk.testing.harness.outcome.Verdict`, and
:func:`assert_gate` raises **two different leaves** for the two non-passing
verdicts so a suite's own report can tell them apart on the type alone.

**No pytest here.** The two shapes that consume this — the async fixtures in
:mod:`application_sdk.testing.harness.fixtures` and a sync composer going
through :func:`~application_sdk.testing.harness.bridge.run_sync` — both want the
gate without importing a test framework, and the runtime scenario suite drives
it from its own scenario format. Everything in this module is a plain async
function over the primitives in
:mod:`application_sdk.testing.harness.waiting`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness._errors import (
    PreconditionsFailedError,
    PreconditionsIndeterminateError,
)
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import (
    Indeterminate,
    Outcome,
    Settled,
    Verdict,
    grade,
)
from application_sdk.testing.harness.temporal import (
    PollerInfo,
    TemporalConnectFailedError,
    TemporalReader,
    TemporalReadFailedError,
    stale_version_pollers,
)
from application_sdk.testing.harness.waiting import poll_until

logger = get_logger(__name__)

__all__ = [
    "GateReport",
    "HealthReading",
    "PollerReading",
    "PreconditionCheck",
    "assert_gate",
    "check_no_stale_pollers",
    "check_worker_health",
    "run_preconditions",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class PreconditionCheck:
    """One thing that must be true before a scenario starts.

    Attributes:
        label: Noun phrase naming what is being required, written for whoever
            reads a red CI leg ("the openapi worker's /server/health"). Carried
            here as well as inside the outcome so the gate can name a check that
            raised before it produced one.
        run: Takes the reading and returns the verdict. A zero-argument async
            callable rather than a probe plus a budget plus predicates, because
            a check is built once by whoever knows its substrate and then run by
            a gate that knows none of it.
    """

    label: str
    run: Callable[[], Coroutine[Any, Any, Outcome[Any]]]


@dataclass(frozen=True, slots=True, kw_only=True)
class GateReport:
    """What the gate observed, and the one verdict it reduces to.

    Attributes:
        verdict: :func:`~application_sdk.testing.harness.outcome.grade` over
            every outcome below.
        outcomes: One outcome per check, in the order the checks were declared.
            Kept whole rather than reduced to a pass/fail list because the
            fields a reader needs — which poller identities were stale, what the
            last HTTP status was — live on the outcome variants.
    """

    verdict: Verdict
    outcomes: tuple[Outcome[Any], ...] = field(default_factory=tuple)

    @property
    def unmet(self) -> tuple[Outcome[Any], ...]:
        """Every check that did not settle, unreadable ones included.

        Returns:
            The non-:class:`~application_sdk.testing.harness.outcome.Settled`
            outcomes, in declaration order.
        """
        return tuple(
            outcome for outcome in self.outcomes if not isinstance(outcome, Settled)
        )

    def summary(self) -> str:
        """Render the unmet checks as one report line.

        Returns:
            A semicolon-separated rendering naming each unmet check, its variant
            and how long it spent. Empty string when everything settled, so a
            caller can put it straight into a message without a conditional.
        """
        return "; ".join(
            f"{outcome.label}: {type(outcome).__name__} after "
            f"{outcome.elapsed.total_seconds():.0f}s "
            f"({outcome.attempts} probe(s))"
            for outcome in self.unmet
        )


async def run_preconditions(checks: Iterable[PreconditionCheck]) -> GateReport:
    """Run every check and grade what they observed.

    Runs the checks **in order and to completion**, rather than stopping at the
    first unmet one. Accumulation is the same choice
    :mod:`application_sdk.testing.harness.outcome` makes and for the same
    reason: a gate that reports only the first thing wrong with an environment
    costs one CI run per fault to diagnose, and the faults an environment gate
    catches arrive in groups — a tenant that was never prepared fails the worker
    check *and* the poller check, and knowing both is what identifies it as one
    cause rather than two.

    A check that raises instead of returning a verdict is recorded as
    :class:`~application_sdk.testing.harness.outcome.Indeterminate` rather than
    propagating. The gate's contract is that it always produces a report: a
    check whose own code broke has told us nothing about the environment, which
    is exactly what that variant means, and letting it propagate would lose the
    verdicts of the checks that already ran.

    Args:
        checks: The checks to run, in the order they should run.

    Returns:
        The report. An empty ``checks`` grades
        :attr:`~application_sdk.testing.harness.outcome.Verdict.PASSED` — a
        suite that declares no preconditions makes no claim about its starting
        state, and inventing an ``INDETERMINATE`` there would force every
        composer to declare a check before it could run a single test.
    """
    outcomes: list[Outcome[Any]] = []
    for check in checks:
        try:
            outcomes.append(await check.run())
        # Exception, not BaseException: a cancelled run is not a failed gate.
        except Exception as error:
            logger.warning(
                "precondition check %r raised instead of returning a verdict — "
                "recording it as indeterminate, so the gate reports every other "
                "check rather than aborting on this one",
                check.label,
                exc_info=True,
            )
            outcomes.append(
                Indeterminate(
                    label=check.label,
                    attempts=0,
                    elapsed=timedelta(0),
                    cause=error,
                )
            )
    return GateReport(verdict=grade(outcomes=outcomes), outcomes=tuple(outcomes))


def assert_gate(report: GateReport) -> None:
    """Raise unless every precondition was met.

    Two leaves rather than one, because the two non-passing verdicts route
    differently and a suite that cannot tell them apart will treat a broken
    environment as a regression:

    * :attr:`~application_sdk.testing.harness.outcome.Verdict.FAILED` — the
      starting state was read, and it was wrong. A ``PRECONDITION`` leaf: state
      has to change before the scenario can run.
    * :attr:`~application_sdk.testing.harness.outcome.Verdict.INDETERMINATE` —
      the starting state could not be read. A ``DEPENDENCY_UNAVAILABLE`` leaf,
      which is retryable by category, so a CI lane that reruns on infrastructure
      failure can act on it without parsing a message.

    Neither is an ``AssertionError``, and that is deliberate: under pytest an
    unmet precondition is reported as an **error** rather than as a failure, so a
    scenario suite's red does not read as "the thing under test regressed" when
    the environment was never fit to test it.

    Args:
        report: What :func:`run_preconditions` produced.

    Raises:
        PreconditionsFailedError: A readable precondition was not met.
        PreconditionsIndeterminateError: A precondition could not be read, and
            none of the readable ones failed.
    """
    if report.verdict is Verdict.PASSED:
        return
    unmet = report.summary()
    labels = ",".join(outcome.label for outcome in report.unmet)
    if report.verdict is Verdict.FAILED:
        raise PreconditionsFailedError(
            message=(
                "the scenario's preconditions were not met, so no work was "
                f"dispatched: {unmet}. This is the starting state, not a "
                "verdict on the thing under test."
            ),
            checks=labels,
            verdict=str(report.verdict),
        )
    raise PreconditionsIndeterminateError(
        message=(
            "the scenario's preconditions could not be read, so no work was "
            f"dispatched and there is no verdict: {unmet}"
        ),
        checks=labels,
        verdict=str(report.verdict),
    )


# ---------------------------------------------------------------------------
# The two built-in checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthReading:
    """One reading of an HTTP health endpoint.

    A reading rather than an exception, which is the load-bearing choice in
    :func:`check_worker_health`: while a worker is still starting, a refused
    connection is the *expected* answer, not a failed read. Modelling it as a
    probe error would make the health poll's first refused connection either an
    absorbed transient (spending the wait's error streak on normal startup) or an
    :class:`~application_sdk.testing.harness.outcome.Indeterminate` — and "the
    worker never came up" is a genuine finding about the thing under test, never
    an unreadable dependency.

    Attributes:
        status: HTTP status the endpoint answered, or ``None`` if it did not
            answer at all.
        error: Transport error as a string when ``status`` is ``None``, else
            empty. Stringified rather than kept as an exception because this
            value goes into a fingerprint and a report line, and the poll keeps
            no per-attempt exception chain.
    """

    status: int | None = None
    error: str = ""

    @property
    def healthy(self) -> bool:
        """Whether this reading is a 2xx.

        Returns:
            True when the endpoint answered with a success status.
        """
        return self.status is not None and 200 <= self.status < 300

    def __str__(self) -> str:
        """Render the reading for a report line or a progress fingerprint."""
        return f"HTTP {self.status}" if self.status is not None else self.error


def check_worker_health(
    url: str,
    *,
    budget: Budget,
    label: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PreconditionCheck:
    """Require that a worker answers 2xx on its health endpoint.

    The connector-side precondition, as a check the gate can run alongside the
    runtime side's. It re-expresses ``BaseE2ETest.assert_worker_up`` over
    :func:`~application_sdk.testing.harness.waiting.poll_until`: the same
    bounded poll of the same endpoint, returning a verdict instead of raising, so
    it can be graded with everything else the gate observed.

    Args:
        url: Health endpoint to poll. A fixed URL from configuration, never a
            value from a payload — it is passed to an HTTP client as-is.
        budget: The wait's allowance.
            :attr:`~application_sdk.testing.harness.budgets.Wait.WORKER_HEALTH`
            in a profile carries the connector-CI numbers.
            ``start_grace`` splits the two diagnoses: with one set, an endpoint
            that never answers at all returns
            :class:`~application_sdk.testing.harness.outcome.NeverStarted` (the
            worker did not deploy) rather than
            :class:`~application_sdk.testing.harness.outcome.Expired` (it
            answered, but never 2xx).
        label: Override for the report line. Defaults to naming the URL.
        transport: HTTP transport to use, or ``None`` for the real one. The seam
            a test drives, so the poll's own behaviour — how a refused
            connection is read, when the wait settles — is checked against a
            real :class:`httpx.AsyncClient` over a scripted transport rather
            than against a patched module global.

    Returns:
        The check. Nothing is polled until the gate runs it.
    """
    check_label = label or f"worker health at {url}"

    async def _run() -> Outcome[HealthReading]:
        # One client for the whole poll, closed when it ends: a fresh client per
        # attempt is the per-iteration-handshake cost the sync bridge exists to
        # avoid, and a health poll is the loop that runs most often.
        async with httpx.AsyncClient(
            follow_redirects=True, transport=transport
        ) as http:

            async def probe() -> HealthReading:
                try:
                    response = await http.get(url, timeout=10.0)
                except (httpx.HTTPError, OSError) as error:
                    # conformance: ignore[E007] the transport failure IS the reading — returned as HealthReading(error=...) and carried into the outcome the caller grades, so nothing is hidden; logging here would emit one line per poll for the normal case of a container still starting
                    return HealthReading(error=f"{type(error).__name__}: {error}")
                return HealthReading(status=response.status_code)

            return await poll_until(
                probe,
                settled=lambda reading: reading.healthy,
                started=lambda reading: reading.status is not None,
                budget=budget,
                label=check_label,
            )

    return PreconditionCheck(label=check_label, run=_run)


@dataclass(frozen=True, slots=True, kw_only=True)
class PollerReading:
    """One reading of who is polling a task queue.

    Attributes:
        pollers: Every poller Temporal reported. Empty is a real answer — the
            observed form of "nothing is polling this queue".
        stale: The subset not on the intended build id, per
            :func:`~application_sdk.testing.harness.temporal.stale_version_pollers`.
    """

    pollers: tuple[PollerInfo, ...] = field(default_factory=tuple)
    stale: tuple[PollerInfo, ...] = field(default_factory=tuple)

    def __str__(self) -> str:
        """Render the reading for a report line or a progress fingerprint."""
        if not self.pollers:
            return "no pollers"
        stale = ",".join(sorted(poller.identity for poller in self.stale))
        return f"{len(self.pollers)} poller(s), stale: {stale or 'none'}"


def check_no_stale_pollers(
    *,
    reader: TemporalReader,
    queue: str,
    namespace: str,
    current_build_id: str | None,
    budget: Budget,
    label: str | None = None,
) -> PreconditionCheck:
    """Require that the queue is polled, and only by the intended build.

    The runtime side's precondition. It is one check rather than two because the
    two halves fail as one condition: a queue polled *only* by stale workers and
    a queue polled by nobody are both "the version I am about to test is not the
    version that will pick this up", and a scenario that dispatched work under
    either would attribute the previous build's behaviour to this one.

    The two halves still report differently, through the wait's own vocabulary:

    * nothing ever polling gives
      :class:`~application_sdk.testing.harness.outcome.NeverStarted` when the
      budget carries a ``start_grace`` — the deployment never arrived;
    * stale pollers that never drain give
      :class:`~application_sdk.testing.harness.outcome.Stalled` when it carries a
      ``stall_timeout``, with the stale identities as the frozen fingerprint —
      the previous scenario did not clean up;
    * an unreadable frontend gives
      :class:`~application_sdk.testing.harness.outcome.Indeterminate`, which is
      the verdict this whole gate exists to keep available.

    Args:
        reader: Read-only Temporal reader.
        queue: Task queue the scenario is about to dispatch onto.
        namespace: Temporal namespace the queue lives in.
        current_build_id: Build id the deployment intends to be serving, or
            ``None`` when versioning is not in use — in which case no poller can
            be stale and the check reduces to "something is polling".
        budget: The wait's allowance. Poll interval, grace and stall window all
            come from here.
        label: Override for the report line. Defaults to naming the queue.

    Returns:
        The check. Nothing is read until the gate runs it.
    """
    check_label = label or f"pollers on task queue {queue}"

    async def probe() -> PollerReading:
        pollers = tuple(await reader.task_queue_pollers(queue, namespace=namespace))
        return PollerReading(
            pollers=pollers,
            stale=tuple(
                stale_version_pollers(pollers, current_build_id=current_build_id)
            ),
        )

    async def _run() -> Outcome[PollerReading]:
        return await poll_until(
            probe,
            settled=lambda reading: bool(reading.pollers) and not reading.stale,
            started=lambda reading: bool(reading.pollers),
            fingerprint=str,
            transient=_temporal_read_is_transient,
            budget=budget,
            label=check_label,
        )

    return PreconditionCheck(label=check_label, run=_run)


def _temporal_read_is_transient(error: BaseException) -> timedelta | None:
    """Classify a Temporal read failure for the poller poll.

    A frontend that could not be reached or could not answer is absorbed as
    transient — out-of-cluster scenarios reach it over a VPN and a vcluster
    tunnel, where a reset connection mid-poll is routine. Nothing else is: a
    ``WorkflowNotFoundError`` or a wrong call signature raises the same
    exception on every attempt, so absorbing it would spend the whole budget
    confirming a bug in the probe.

    Args:
        error: The exception the probe raised.

    Returns:
        Zero backoff (honour the poll's own cadence) for a reachability failure;
        ``None`` for anything else, which propagates.
    """
    if isinstance(error, (TemporalConnectFailedError, TemporalReadFailedError)):
        return timedelta(0)
    return None
