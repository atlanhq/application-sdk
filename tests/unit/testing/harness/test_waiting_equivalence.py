"""``poll_native_status`` and ``poll_until``, on identical scripted readings.

FND-227 was an *extraction*: the start-grace latch, the progress watchdog and the
transient streak with its retry-after budget all already worked — as interleaved
statements inside ``poll_native_status``, coupled to the AE ``native-status``
wire shape, the ``DAGNodeStatus`` vocabulary and a node-glyph summary string.
This file was the differential evidence that the generic primitive preserved
them, run before the live loop was touched.

**FND-240 consolidated the two, so this file changed job.** ``poll_native_status``
*is* :func:`~application_sdk.testing.harness.waiting.poll_until` now, and a
comparison of a loop against itself proves nothing. So the pre-conversion numbers
were captured and are pinned here as :data:`_GOLDEN` — the same three observable
things per scenario, **which verdict, after how many probes, having slept exactly
which gaps**, as produced by the hand-rolled loop. That is what makes "behaviour
preserving" a fact rather than a claim in a commit message, and it survives the
conversion in a way a self-comparison would not.

The sleep sequence is the load-bearing column: it is what catches a retry-after
clamp, an off-by-one in the budget accounting or a lost interval floor, none of
which change the verdict.

Two comparisons still run against ``poll_until`` directly, and both remain
non-trivial after the conversion because :meth:`AEClient.poll_native_status`
reaches the primitive through its *own* conversion — the keyword arguments into a
:class:`~application_sdk.testing.harness.budgets.Budget`, the AE shape into four
callables, the verdict back into an AE leaf:

* ``test_the_primitive_and_the_live_loop_agree`` pins that conversion against the
  hand-written callables below. A ``stall_grace_seconds`` that stopped arming
  ``start_grace``, or a classifier that stopped rounding, diverges here.
* the budget both sides run on is ``CONNECTOR_CI[Wait.AE_RUN]``, converted back
  to the live loop's keyword arguments — so this is a check on the profile too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import patch

import pytest

from application_sdk.errors import AppError
from application_sdk.testing.e2e._errors import (
    AtlanApiHttpError,
    AtlanApiTimeoutError,
    AutomationEngineNotDispatchingError,
    DAGProgressStalledError,
    NoWorkerOnTaskQueueError,
)
from application_sdk.testing.e2e.client import (
    AEWorkflowClient,
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.automation_engine.wire import (
    node_glyph as _node_glyph,
)
from application_sdk.testing.harness.budgets import CONNECTOR_CI, Wait
from application_sdk.testing.harness.waiting import poll_until

_RUN_ID = "test-run-123"
_AE = CONNECTOR_CI.budgets[Wait.AE_RUN]

#: The two vocabularies, side by side — and since FND-240 this table *is* the
#: independent statement of what
#: :func:`~application_sdk.testing.harness.automation_engine.client._dag_run_verdict`
#: implements. Every live outcome, returned result or raised leaf, has exactly one
#: harness verdict: four connector-specific leaves collapse to the three generic
#: verdicts that carry the *diagnosis*, while the remediation advice those leaves
#: exist for stays with them. Written here rather than imported from the code
#: under test, so an inverted branch in that mapping has somewhere to fail.
_SAME_VERDICT = {
    "terminal": "Settled",
    "timed_out": "Expired",
    NoWorkerOnTaskQueueError.__name__: "NeverStarted",
    AutomationEngineNotDispatchingError.__name__: "NeverStarted",
    DAGProgressStalledError.__name__: "Stalled",
    # The streak gave up. The live loop re-raises the origin's own error; the
    # primitive declines to call an unreadable dependency a verdict on the run.
    AtlanApiHttpError.__name__: "Indeterminate",
    # The budget expired without one usable response.
    AtlanApiTimeoutError.__name__: "Indeterminate",
}


# ---------------------------------------------------------------------------
# Building readings
# ---------------------------------------------------------------------------


def _result(run_status: DAGRunStatus, *node_statuses: DAGNodeStatus) -> DAGRunResult:
    """One ``native-status`` reading with one node per given status."""
    return DAGRunResult(
        run_id=_RUN_ID,
        workflow_slug="slug",
        status=run_status,
        nodes=[
            DAGNodeResult(
                name=f"node{index}",
                status=status,
                started_at_ms=None,
                completed_at_ms=None,
                error_message=None,
            )
            for index, status in enumerate(node_statuses)
        ],
    )


def _blip(retry_after: float | None = None) -> AtlanApiHttpError:
    """The 500 AE returns for a few seconds when the tenant's Temporal blips."""
    return AtlanApiHttpError(
        message="AE-COMMON-500-01: An unexpected error occurred",
        target="GET /api/service/package-workflows/native-status HTTP 500",
        retry_after_seconds=retry_after,
    )


Script = list[DAGRunResult | Exception]


# ---------------------------------------------------------------------------
# The AE shape, as the four callables the primitive takes in
# ---------------------------------------------------------------------------


def _ae_settled(result: DAGRunResult) -> bool:
    return result.status.is_terminal


def _ae_started(result: DAGRunResult) -> bool:
    """A node has started once it leaves the not-started set.

    The check is on node-level start, not the run status: the parent AE workflow
    runs on the always-on automation-engine queue, so the run flips to
    ``Running`` even when the connector's ``extract`` node is stuck because
    nothing polls its task queue.
    """
    return any(not node.status.is_not_started for node in result.nodes)


def _ae_fingerprint(result: DAGRunResult) -> str:
    """The node-glyph summary the live loop already uses as its progress mark."""
    return " ".join(_node_glyph(node) for node in result.nodes)


def _ae_transient(exc: BaseException) -> timedelta | None:
    """Absorb any ``AppError``, honouring an origin ``Retry-After``.

    Rounding up is the classifier's job, not the primitive's: the live
    ``_retry_gap`` takes ``math.ceil`` because its input is an HTTP header in
    whole seconds, whereas the primitive is handed a ``timedelta`` and has no
    business quantising it. Doing it here is what keeps the two gap sequences
    identical.
    """
    if not isinstance(exc, AppError):
        return None
    requested = exc.retry_after_seconds if isinstance(exc, AtlanApiHttpError) else None
    if requested is None or requested <= 0:
        return timedelta(0)
    return timedelta(seconds=math.ceil(requested))


# ---------------------------------------------------------------------------
# Running one script through both loops
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Observed:
    """What one loop did with a script — the whole comparable surface.

    Attributes:
        verdict: The harness verdict name, live outcomes mapped through
            :data:`_SAME_VERDICT`.
        attempts: How many times the loop probed.
        slept: Every gap it slept, in order.
    """

    verdict: str
    attempts: int
    slept: tuple[float, ...]


@dataclass
class _Replay:
    """Serves a script in order, repeating its last item forever."""

    script: Script
    calls: int = field(default=0)

    def next(self) -> DAGRunResult:
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


async def _live(script: Script) -> Observed:
    """Run the script through ``poll_native_status``.

    Awaited on
    :class:`~application_sdk.testing.harness.automation_engine.client.AEClient`
    rather than called through ``AEWorkflowClient``: since child F the facade is
    a one-line ``run_sync`` shim, and ``run_sync`` refuses to re-enter a running
    loop — which this test is. The loop under comparison is the coroutine
    either way.
    """
    client = AEWorkflowClient(
        tenant_url="https://tenant.example.com", api_token="tok-test"
    )
    replay = _Replay(script)
    grace = _AE.start_grace
    stall = _AE.stall_timeout
    assert grace is not None and stall is not None, "the AE_RUN profile arms both"

    async def _read(_run_id: str) -> DAGRunResult:
        return replay.next()

    with patch.object(client._ae, "get_native_status", side_effect=_read):
        with fake_clock() as clock:
            try:
                result = await client._ae.poll_native_status(
                    _RUN_ID,
                    interval_seconds=int(_AE.poll_interval.total_seconds()),
                    timeout_seconds=int(_AE.timeout.total_seconds()),
                    max_transient_failures=_AE.max_transient_failures,
                    stall_grace_seconds=int(grace.total_seconds()),
                    progress_stall_seconds=int(stall.total_seconds()),
                    stall_task_queue="atlan-openapi-e2e-full-ci",
                )
            except AppError as error:
                raised = type(error).__name__
                return Observed(
                    verdict=_SAME_VERDICT[raised],
                    attempts=replay.calls,
                    slept=tuple(clock.slept),
                )
            returned = "timed_out" if result.timed_out else "terminal"
            return Observed(
                verdict=_SAME_VERDICT[returned],
                attempts=replay.calls,
                slept=tuple(clock.slept),
            )


async def _harness(script: Script) -> Observed:
    """Run the same script through ``poll_until``, AE shape passed in."""
    replay = _Replay(script)

    async def probe() -> DAGRunResult:
        return replay.next()

    with fake_clock() as clock:
        outcome = await poll_until(
            probe,
            settled=_ae_settled,
            started=_ae_started,
            fingerprint=_ae_fingerprint,
            transient=_ae_transient,
            budget=_AE,
            label=f"AE run {_RUN_ID}",
        )
    return Observed(
        verdict=type(outcome).__name__,
        attempts=outcome.attempts,
        slept=tuple(clock.slept),
    )


# ---------------------------------------------------------------------------
# The scripts
# ---------------------------------------------------------------------------

_RUNNING_NODE = _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING)
_PENDING_NODE = _result(DAGRunStatus.RUNNING, DAGNodeStatus.PENDING)
_NOT_DISPATCHED = _result(DAGRunStatus.PENDING, DAGNodeStatus.PENDING)
_SUCCEEDED = _result(DAGRunStatus.SUCCEEDED, DAGNodeStatus.SUCCEEDED)
_FAILED = _result(DAGRunStatus.FAILED, DAGNodeStatus.FAILED)

#: Alternates its glyph summary every poll, so the watchdog never closes and the
#: run reaches its ceiling still visibly progressing.
_PROGRESSING: Script = [
    _result(DAGRunStatus.RUNNING, DAGNodeStatus.RUNNING, status)
    for _ in range(40)
    for status in (DAGNodeStatus.PENDING, DAGNodeStatus.RUNNING)
]

_SCENARIOS: dict[str, Script] = {
    "settles-immediately": [_SUCCEEDED],
    "settles-after-progress": [_RUNNING_NODE, _RUNNING_NODE, _SUCCEEDED],
    "settles-on-a-failed-run": [_RUNNING_NODE, _FAILED],
    "no-node-ever-starts": [_PENDING_NODE],
    "the-run-is-never-dispatched": [_NOT_DISPATCHED],
    "started-then-frozen": [_RUNNING_NODE],
    "progressing-past-the-ceiling": _PROGRESSING,
    "blips-below-the-streak": [_blip(), _blip(), _blip(), _blip(), _SUCCEEDED],
    "the-streak-gives-up": [_blip()],
    "a-blip-that-asks-for-a-backoff": [_blip(retry_after=30), _SUCCEEDED],
    "a-backoff-request-below-the-interval": [_blip(retry_after=2), _SUCCEEDED],
    "a-pathological-backoff-request": [_blip(retry_after=900), _SUCCEEDED],
    "a-fractional-backoff-request": [_blip(retry_after=30.4), _SUCCEEDED],
    "blips-that-each-ask-for-a-backoff": [
        _blip(retry_after=120),
        _blip(retry_after=120),
        _blip(retry_after=120),
        _SUCCEEDED,
    ],
    "a-blip-then-progress-then-more-blips": [
        _blip(),
        _blip(),
        _RUNNING_NODE,
        _blip(),
        _blip(),
        _SUCCEEDED,
    ],
}


#: What the **hand-rolled** loop did with each script, captured before FND-240
#: replaced it with ``poll_until``. Not derived from the current code: these are
#: transcribed from a run against the pre-conversion ``poll_native_status``, so
#: they cannot drift with the implementation they exist to constrain.
#:
#: A change here is a change in what a connector's e2e run does — how many times
#: it asks AE, and how long it waits between asks. That is reviewable on its own
#: terms; silently absorbing it into a refactor is what this table prevents.
_GOLDEN: dict[str, Observed] = {
    "a-backoff-request-below-the-interval": Observed("Settled", 2, (10,)),
    "a-blip-that-asks-for-a-backoff": Observed("Settled", 2, (30,)),
    "a-blip-then-progress-then-more-blips": Observed("Settled", 6, (10,) * 5),
    "a-fractional-backoff-request": Observed("Settled", 2, (31,)),
    "a-pathological-backoff-request": Observed("Settled", 2, (120,)),
    "blips-below-the-streak": Observed("Settled", 5, (10,) * 4),
    "blips-that-each-ask-for-a-backoff": Observed("Settled", 4, (120, 120, 80)),
    "no-node-ever-starts": Observed("NeverStarted", 19, (10,) * 18),
    "progressing-past-the-ceiling": Observed("Expired", 60, (10,) * 59),
    "settles-after-progress": Observed("Settled", 3, (10, 10)),
    "settles-immediately": Observed("Settled", 1, ()),
    "settles-on-a-failed-run": Observed("Settled", 2, (10,)),
    "started-then-frozen": Observed("Stalled", 31, (10,) * 30),
    "the-run-is-never-dispatched": Observed("NeverStarted", 19, (10,) * 18),
    "the-streak-gives-up": Observed("Indeterminate", 5, (10,) * 4),
}


def test_every_scenario_has_a_golden() -> None:
    """A script with no pinned numbers is a script this file does not constrain.

    Cheap, and it is the failure mode the table invites: adding a scenario to
    ``_SCENARIOS`` and forgetting the pin leaves it silently unchecked by the
    only test that knows what the old loop did.
    """
    assert sorted(_GOLDEN) == sorted(_SCENARIOS)


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_the_live_loop_still_does_what_it_did_before(name: str) -> None:
    """Same verdict, same probe count, same sleep sequence as the old loop."""
    assert await _live(_SCENARIOS[name]) == _GOLDEN[name]


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_the_primitive_and_the_live_loop_agree(name: str) -> None:
    """The live loop's own conversion matches the hand-written one.

    ``poll_native_status`` builds the :class:`Budget` and the four callables from
    its keyword arguments; :func:`_harness` writes them out by hand. Same script
    through both is what catches a guard that stopped being armed or a classifier
    that stopped rounding — neither of which the golden table alone would find,
    since it is the live loop's own output on both sides of that comparison.
    """
    script = _SCENARIOS[name]
    assert await _harness(script) == await _live(script)


# ---------------------------------------------------------------------------
# What the comparison would miss on its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "verdict"),
    [
        ("settles-immediately", "Settled"),
        ("no-node-ever-starts", "NeverStarted"),
        ("the-run-is-never-dispatched", "NeverStarted"),
        ("started-then-frozen", "Stalled"),
        ("progressing-past-the-ceiling", "Expired"),
        ("the-streak-gives-up", "Indeterminate"),
    ],
)
async def test_each_verdict_is_actually_reached(name: str, verdict: str) -> None:
    """Two loops agreeing on the wrong answer is still agreement.

    Pinning the verdict each script is *supposed* to produce is what stops the
    parametrised comparison above degenerating into "both returned ``Expired``,
    because neither guard fired at all".
    """
    assert (await _harness(_SCENARIOS[name])).verdict == verdict


async def test_a_non_apperror_propagates_from_both_loops() -> None:
    """The classifier absorbs ``AppError`` and nothing else, matching the live
    ``except AppError``: a deterministic bug in the probe must not be waited out.

    **The probe count is what asserts "not waited out"**, and without it this
    test passes on a broken classifier. Verified mechanically: with
    ``_absorb_ae_blip``'s ``isinstance(error, AppError)`` narrowing removed, the
    ``ValueError`` is absorbed, the streak runs to
    ``max_transient_failures``, and ``_dag_run_verdict`` then re-raises
    ``outcome.cause`` — which is the same ``ValueError``. So
    ``pytest.raises(ValueError)`` alone is satisfied by the mutation it exists to
    catch; only the attempt count distinguishes failing on the first probe from
    waiting out five.
    """
    bug = ValueError("a bug in the probe, not a blip")

    # The primitive, driven with the AE shape written out by hand.
    primitive = _Replay([bug])

    async def probe() -> DAGRunResult:
        return primitive.next()

    with fake_clock(), pytest.raises(ValueError):
        await poll_until(
            probe,
            settled=_ae_settled,
            started=_ae_started,
            fingerprint=_ae_fingerprint,
            transient=_ae_transient,
            budget=_AE,
            label=f"AE run {_RUN_ID}",
        )
    assert primitive.calls == 1, (
        f"poll_until probed {primitive.calls} times; a deterministic probe bug "
        "must fail on the first, not be waited out"
    )

    # The live loop, through its own Budget and classifier conversion.
    live = _Replay([bug])
    client = AEWorkflowClient(
        tenant_url="https://tenant.example.com", api_token="tok-test"
    )

    async def _read(_run_id: str) -> DAGRunResult:
        return live.next()

    grace, stall = _AE.start_grace, _AE.stall_timeout
    assert grace is not None and stall is not None
    with (
        patch.object(client._ae, "get_native_status", side_effect=_read),
        fake_clock(),
        pytest.raises(ValueError),
    ):
        await client._ae.poll_native_status(
            _RUN_ID,
            interval_seconds=int(_AE.poll_interval.total_seconds()),
            timeout_seconds=int(_AE.timeout.total_seconds()),
            max_transient_failures=_AE.max_transient_failures,
            stall_grace_seconds=int(grace.total_seconds()),
            progress_stall_seconds=int(stall.total_seconds()),
        )
    assert live.calls == 1, (
        f"poll_native_status probed {live.calls} times; with the classifier's "
        "AppError narrowing removed this reaches 5 and still raises ValueError, "
        "which is why the count is the assertion and not the exception type"
    )


async def test_the_stall_verdict_names_the_summary_that_froze() -> None:
    """The one field the connector leaf carried as prose and the verdict carries
    as data — the reason ``Stalled`` is worth having at all."""
    replay = _Replay([_RUNNING_NODE])

    async def probe() -> DAGRunResult:
        return replay.next()

    with fake_clock():
        outcome = await poll_until(
            probe,
            settled=_ae_settled,
            started=_ae_started,
            fingerprint=_ae_fingerprint,
            transient=_ae_transient,
            budget=_AE,
            label=f"AE run {_RUN_ID}",
        )
    assert outcome.fingerprint == _ae_fingerprint(_RUNNING_NODE)  # type: ignore[union-attr]
    assert outcome.stall_window == _AE.stall_timeout  # type: ignore[union-attr]
