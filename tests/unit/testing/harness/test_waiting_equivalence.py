"""``poll_until`` against ``poll_native_status``, on identical scripted readings.

FND-227 is an *extraction*: the start-grace latch, the progress watchdog and the
transient streak with its retry-after budget all already work — as interleaved
statements inside ``AEWorkflowClient.poll_native_status``, coupled to the AE
``native-status`` wire shape, the ``DAGNodeStatus`` vocabulary and a node-glyph
summary string. Claiming the generic primitive preserves them needs evidence,
and "the 58 existing client tests still pass" is not that evidence: those tests
exercise the live loop, which this PR does not touch.

So the evidence is differential. Each scenario below is one script — a list of
readings and errors — fed to *both* loops:

* the live one, through a patched ``get_native_status``;
* :func:`~application_sdk.testing.harness.waiting.poll_until`, with the AE shape
  supplied as the four callables the primitive takes in.

Both run under :func:`~application_sdk.testing.harness._poll.fake_clock`, so the
comparison covers three observable things and not just the answer: **which
verdict, after how many probes, having slept exactly which gaps.** The sleep
sequence is the load-bearing one — it is what catches a retry-after clamp, an
off-by-one in the budget accounting or a lost interval floor, none of which
change the verdict.

The budget both sides run on is ``CONNECTOR_CI[Wait.AE_RUN]``, converted back to
the live loop's keyword arguments. That makes this a check on the profile too: if
child B's lift of those numbers were wrong, the two loops would diverge here.

Re-expressing the live loop *on* the primitive is child D (FND-240) — its client
is synchronous until child F converts it, and bridging a sync method onto an
async primitive in the meantime would be a workaround built to be deleted. This
file is what stands in for that proof until then, and it costs the connector path
no diff at all.
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
    _node_glyph,
)
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.budgets import CONNECTOR_CI, Wait
from application_sdk.testing.harness.waiting import poll_until

_RUN_ID = "test-run-123"
_AE = CONNECTOR_CI.budgets[Wait.AE_RUN]

#: The two vocabularies, side by side. Every live outcome — a returned result or
#: a raised leaf — has exactly one harness verdict, and the mapping is the
#: extraction's actual claim: four connector-specific leaves collapse to the
#: three generic verdicts that carry the *diagnosis*, while the remediation
#: advice those leaves exist for stays with them.
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


def _live(script: Script) -> Observed:
    """Run the script through ``poll_native_status``."""
    client = AEWorkflowClient(
        tenant_url="https://tenant.example.com", api_token="tok-test"
    )
    replay = _Replay(script)
    grace = _AE.start_grace
    stall = _AE.stall_timeout
    assert grace is not None and stall is not None, "the AE_RUN profile arms both"

    with patch.object(
        client, "get_native_status", side_effect=lambda _id: replay.next()
    ):
        with fake_clock() as clock:
            try:
                result = client.poll_native_status(
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


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
async def test_the_primitive_and_the_live_loop_agree(name: str) -> None:
    """Same script, same verdict, same probe count, same sleep sequence."""
    script = _SCENARIOS[name]
    assert await _harness(script) == _live(script)


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
    """
    with pytest.raises(ValueError):
        await _harness([ValueError("a bug in the probe, not a blip")])
    with pytest.raises(ValueError):
        _live([ValueError("a bug in the probe, not a blip")])


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
