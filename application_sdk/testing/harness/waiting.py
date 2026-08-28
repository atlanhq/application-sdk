"""The two bounded-wait primitives every harness loop is expressed over.

**This is an extraction, not an invention.** Both guards :func:`poll_until`
needs already work — they are interleaved statements inside
``testing/e2e/client.py``'s ``poll_native_status``, coupled to the Automation
Engine ``native-status`` wire shape, the ``DAGNodeStatus`` vocabulary, and a
node-glyph summary *string* used as the progress fingerprint. The generic
primitive underneath is: *bounded poll + caller-supplied progress fingerprint +
started/settled predicates + start-grace latch + no-change watchdog + transient-
error streak with a retry-after budget.* Only the fingerprint and the predicates
are instance-specific, and they are passed in — which is why no fixed shared
verb is needed and why ``ClusterReader`` never has to grow everyone's states.

The clock, the sleeping and the off-by-one live in
:mod:`application_sdk.testing.harness._poll` (``until_deadline`` /
``until_deadline_async``, monotonic, with the gap re-clamped against the clock
read *after* the probe). :func:`poll_until` is built on that rather than on a
second deadline implementation; that private module moved here from
``testing/e2e/`` as part of child C, because the harness cannot import from the
package child H is about to re-express over it.

**Child D (FND-240) brought the bounded loops across**, one at a time. Which
primitive each one reaches for is not uniform, and the split is not arbitrary:

* :func:`poll_until` where the probe is already ``async`` and there is a real
  verdict to grade — ``poll_native_status`` (the loop these guards were
  extracted from in the first place), ``atlas.poll_for_connection``,
  ``workflows.wait_for_workflow``, ``AEClient.wait_for_slug``.
* :func:`~application_sdk.testing.harness._poll.until_deadline` — the deadline
  arithmetic alone — where the probe is *synchronous* and supplied by a
  connector suite or a local test client. Reaching an async primitive from
  there would mean blocking the bridge's loop inside someone else's write, or
  offloading it to a thread: workarounds built to be deleted when child H moves
  the sync boundary up to ``BaseE2ETest``'s public methods. Four loops in
  ``testing/e2e/base.py`` and one in ``testing/integration/runner.py`` take that
  shape.

What is uniform is that no loop in the harness owns a deadline any more, and
there is one clock rather than three.

``tests/unit/testing/harness/test_waiting_equivalence.py`` is the evidence that
the biggest of those conversions preserved behaviour. It was written *before* it,
as a differential against the hand-rolled loop; the numbers that comparison
produced — verdict, probe count and the exact sleep sequence for fifteen scripted
runs — are now frozen there as a golden table, which is a claim a
self-comparison could not make once both sides became one loop.

:func:`hold_stable` is **new**, and still has no caller in this repo. Every
bounded loop the harness has — the twelve child D brought across included — is
"wait until"; not one is "assert stays". The negative assertion is
what the runtime scaling scenarios turn on — "a busy pod is never scaled away",
"it must not scale down while a long activity is still running", and the whole
steady-state group — because most scaling bugs are wrong *actions* rather than
wrong states.

**Neither primitive raises on a failed wait.** Both return an
:mod:`~application_sdk.testing.harness.outcome` variant; see that module for
why, and :func:`~application_sdk.testing.harness.outcome.assert_settled` for the
raise-on-failure adapter the connector suites keep. The one exception is an
*unclassified* probe exception, which propagates — see ``transient`` below.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Generic, TypeAlias, TypeVar

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness._poll import until_deadline_async
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import (
    Expired,
    Indeterminate,
    NeverStarted,
    Outcome,
    Settled,
    Stalled,
)

logger = get_logger(__name__)

__all__ = ["Classifier", "Probe", "hold_stable", "poll_until"]

T = TypeVar("T")

#: One reading of whatever is being waited on. Async by construction (decision
#: D1): there is no synchronous probe anywhere in the harness.
Probe: TypeAlias = Callable[[], Awaitable[T]]

#: Turns a probe exception into the backoff to honour before retrying, or
#: ``None`` for "not transient — this one is terminal". Per backend, never
#: fixed at the primitive: a kubectl read over a tunnel and an in-process HTTP
#: call have different normal failure rates.
Classifier: TypeAlias = Callable[[BaseException], "timedelta | None"]

#: Cap on the rendered reading a :func:`hold_stable` violation carries as its
#: fingerprint. A violating pod list is a report line, not a payload dump.
_RENDER_LIMIT = 120


@dataclass(frozen=True, slots=True)
class _Observation(Generic[T]):
    """A reading that was actually taken, wrapped so its absence has a spelling.

    Both primitives have to answer "did any probe ever succeed?", and neither may
    answer it with ``last_value is None``: ``Probe[T]`` puts no bound on ``T``, so
    ``None`` is a perfectly good reading — a ``ClusterReader`` returning "no such
    deployment", a status field that is legitimately null. Deciding on ``is None``
    turns a successful ``None`` into "never observed", which reports a hold that
    held as :class:`~application_sdk.testing.harness.outcome.Indeterminate` and a
    poll that read its target as never having read it.

    So the holder is the sentinel: ``None`` here means *no observation object*,
    which the probe cannot produce, while ``_Observation(None)`` is a reading of
    ``None``. Same reasoning, and the same shape, as
    :class:`~application_sdk.testing.harness.expectations.Unreadable` — a failed
    read may not borrow the vocabulary of a successful one.

    Attributes:
        value: The reading, whatever ``T`` is.
    """

    value: T


def _last(observation: _Observation[T] | None) -> T | None:
    """Unwrap an observation for an outcome's ``last`` field.

    ``Outcome.last`` is typed ``T | None`` and so cannot itself distinguish a
    reading of ``None`` from no reading at all. That conflation is confined to
    the report: no *decision* in this module is taken on it — see
    :class:`_Observation`.
    """
    return None if observation is None else observation.value


def _seconds(value: timedelta | None) -> float | None:
    """Return *value* in seconds, or ``None``."""
    return None if value is None else value.total_seconds()


def _render(value: object) -> str:
    """Summarise a reading for a report line, bounded so it stays one line."""
    text = repr(value)
    if len(text) <= _RENDER_LIMIT:
        return text
    return f"{text[: _RENDER_LIMIT - 1]}…"


def _honoured_gap(
    requested: timedelta,
    *,
    floor: float,
    cap: float | None,
    budget_left: float,
) -> float:
    """Pick the gap before the next probe, honouring the origin's request.

    Mirrors ``testing/e2e/client.py``'s ``_retry_gap``, with the two bounds that
    were module constants there now read off the :class:`Budget`:

    * ``cap`` (:attr:`Budget.max_retry_after`) bounds any *single* wait, so one
      pathological ``Retry-After`` cannot hang a CI leg inside its own backoff;
    * ``budget_left`` (what is left of :attr:`Budget.retry_after_budget`) bounds
      the *total* above-the-floor waiting across the whole wait.

    Never returns less than ``floor``: honouring a hint may lengthen the gap the
    loop already guaranteed, never shorten it. A wait with no
    :attr:`Budget.retry_after_budget` therefore degrades cleanly to the fixed
    interval, which is exactly what "honour no origin backoff" should do — one
    rule, not a second branch.

    Unlike ``_retry_gap`` this does **not** round the request up to a whole
    second. There the input was an HTTP ``Retry-After`` header, integer seconds
    by definition; here it is a ``timedelta`` the classifier chose, and
    quantising someone else's duration is not the primitive's call. A classifier
    reading that header rounds on the way in.

    Args:
        requested: What the origin asked for. Non-positive means "nothing
            usable", and the floor stands.
        floor: The loop's own fixed gap.
        cap: Ceiling on this single wait, or ``None`` for no ceiling.
        budget_left: Above-the-floor seconds still permitted in this wait.

    Returns:
        The gap to take, before :meth:`Attempt.sleep_next` clamps it again
        against the residual deadline.
    """
    wanted = requested.total_seconds()
    if wanted <= 0:
        return floor
    allowed = min(wanted, max(budget_left, 0.0))
    if cap is not None:
        allowed = min(allowed, cap)
    return max(floor, allowed)


async def poll_until(
    probe: Probe[T],
    *,
    settled: Callable[[T], bool],
    started: Callable[[T], bool] | None = None,
    fingerprint: Callable[[T], str] | None = None,
    transient: Classifier | None = None,
    budget: Budget,
    label: str,
) -> Outcome[T]:
    """Poll until *settled*, or until the budget, grace or watchdog says stop.

    Args:
        probe: Takes one reading. Called once per poll.
        settled: True when the reading is the terminal state being waited for.
            Returns :class:`~application_sdk.testing.harness.outcome.Settled`.
        started: True once work has demonstrably begun. Until it returns True
            the start-grace latch applies, and its expiry gives
            :class:`~application_sdk.testing.harness.outcome.NeverStarted`
            rather than a generic timeout — dispatch failures and slow work are
            different diagnoses. ``None`` means "started on the first reading".
            A latch, not a level: work that starts and finishes between two
            polls still counts as started.
        fingerprint: Reduces a reading to a comparable progress string. When it
            stops changing for ``budget.stall_timeout`` the wait returns
            :class:`~application_sdk.testing.harness.outcome.Stalled` carrying
            the frozen fingerprint. ``None`` disables the watchdog regardless of
            the budget, because there is nothing to compare.
        transient: Classifies a probe exception. Returns a backoff to honour
            (bounded by ``budget.max_retry_after`` and
            ``budget.retry_after_budget``) to absorb it as transient, or
            ``None`` to treat it as terminal. An unclassified exception
            propagates: a deterministic bug in the probe itself — a wrong call
            signature, a bad config value — raises the same exception on every
            attempt, so waiting out the budget only delays the failure. Passing
            no classifier at all therefore means "every probe error is a bug in
            the probe", which is right for an in-process read and wrong for a
            kubectl read over a tunnel — hence per backend, not fixed at the
            primitive (FND-227's 2026-08-17 amendment).
        budget: The wait's whole allowance. See
            :class:`~application_sdk.testing.harness.budgets.Budget`.
        label: Noun phrase naming the concrete target, for the report and the
            heartbeat line. Written for whoever reads a red CI leg.

    Returns:
        The verdict. Never raises on a failed wait — see
        :mod:`application_sdk.testing.harness.outcome` for why, and
        :func:`~application_sdk.testing.harness.outcome.assert_settled` for the
        raise-on-failure adapter.

        A budget that expires without one successful reading returns
        :class:`~application_sdk.testing.harness.outcome.Indeterminate`, not
        :class:`~application_sdk.testing.harness.outcome.Expired`: "it did not
        finish in time" is a claim about the thing under test, and a wait that
        never read it is not entitled to make one.

    Raises:
        BaseException: Whatever ``probe`` raised, when ``transient`` does not
            classify it as worth absorbing.
    """
    interval_seconds = budget.poll_interval.total_seconds()
    grace_seconds = _seconds(budget.start_grace)
    # A watchdog with nothing to compare is not a watchdog. The fingerprint
    # decides whether the budget's window is armed, not the other way round.
    stall_seconds = _seconds(budget.stall_timeout) if fingerprint else None
    retry_after_left = (budget.retry_after_budget or timedelta(0)).total_seconds()
    max_retry_after = _seconds(budget.max_retry_after)

    # ``started=None`` is "started on the first reading", which is the same
    # latch already closed — so the grace window can never fire, and there is no
    # separate no-predicate branch below.
    has_started = started is None
    streak = 0
    attempts = 0
    elapsed = 0.0
    observation: _Observation[T] | None = None
    last_error: BaseException | None = None
    last_fingerprint: str | None = None
    last_progress = 0.0

    async for attempt in until_deadline_async(
        budget.timeout.total_seconds(),
        interval_seconds,
        label=label,
        heartbeat_seconds=(budget.heartbeat or timedelta(0)).total_seconds(),
    ):
        attempts = attempt.number
        elapsed = attempt.elapsed
        try:
            value = await probe()
        # Exception, not BaseException: a cancelled test is not a failed read,
        # and asyncio.CancelledError must reach the task that sent it.
        except Exception as error:
            backoff = transient(error) if transient is not None else None
            if backoff is None:
                raise
            last_error = error
            streak += 1
            if streak >= budget.max_transient_failures:
                # WARNING, not ERROR: an unreadable dependency is explicitly not
                # a verdict on the thing under test, and the caller is handed
                # the cause to grade. Reporting it as an error here would red a
                # dashboard for something this function declines to call a
                # failure.
                logger.warning(
                    "giving up on %s after %d consecutive probe error(s) in "
                    "%.0fs — no verdict, the read itself failed",
                    label,
                    streak,
                    elapsed,
                    exc_info=True,
                )
                return Indeterminate(
                    label=label,
                    attempts=attempts,
                    elapsed=timedelta(seconds=elapsed),
                    cause=error,
                    transient_failures=streak,
                    last=_last(observation),
                )
            # Back off for as long as the origin asked when it said so, rather
            # than for the poll cadence: an overloaded origin answering
            # "retry_after: 120" would otherwise burn the whole streak inside
            # its own wait window. ``sleep_next`` clamps to the residual budget
            # and reports the gap actually taken.
            gap = _honoured_gap(
                backoff,
                floor=interval_seconds,
                cap=max_retry_after,
                budget_left=retry_after_left,
            )
            retry_after_left -= gap - interval_seconds
            slept = attempt.sleep_next(gap)
            # conformance: ignore[L006] fires only on an absorbed probe error, not per iteration; a streak that ends in a returned Indeterminate leaves no other record of how long the origin was asked to be waited for
            logger.warning(
                "transient error probing %s (streak %d/%d) — sleeping %.0fs "
                "and retrying",
                label,
                streak,
                budget.max_transient_failures,
                slept,
                exc_info=True,
            )
            continue

        streak = 0
        observation = _Observation(value)
        if not has_started and started is not None and started(value):
            has_started = True
        if fingerprint is not None:
            mark = fingerprint(value)
            if mark != last_fingerprint:
                last_fingerprint = mark
                last_progress = elapsed
        if settled(value):
            return Settled(
                label=label,
                attempts=attempts,
                elapsed=timedelta(seconds=elapsed),
                value=value,
            )
        # Fail fast when nothing has started inside the grace window. Checked
        # only after a reading that succeeded: a probe that could not be read
        # has not shown that nothing started, it has shown nothing at all.
        if not has_started and grace_seconds is not None and elapsed >= grace_seconds:
            return NeverStarted(
                label=label,
                attempts=attempts,
                elapsed=timedelta(seconds=elapsed),
                grace=timedelta(seconds=grace_seconds),
                last=value,
            )
        # Progress watchdog: something started, then the fingerprint froze for
        # the whole window. Turns an indefinite hang into a self-terminating
        # failure that names what stopped moving.
        if (
            has_started
            and stall_seconds is not None
            and (elapsed - last_progress) >= stall_seconds
        ):
            return Stalled(
                label=label,
                attempts=attempts,
                elapsed=timedelta(seconds=elapsed),
                # The observed quiet gap, not the configured window: they differ
                # whenever a probe overran its own interval, and the observed one
                # is the number that answers "how long has it been frozen?".
                stall_window=timedelta(seconds=elapsed - last_progress),
                fingerprint=last_fingerprint or "",
                last=value,
            )

    # Never *observed*, not "the last reading happened to be None" — a wait that
    # read its target and one that never could are different claims, and a
    # `Probe[None]` makes them identical to an `is None` test.
    if observation is None and last_error is not None:
        return Indeterminate(
            label=label,
            attempts=attempts,
            elapsed=timedelta(seconds=elapsed),
            cause=last_error,
            transient_failures=streak,
        )
    return Expired(
        label=label,
        attempts=attempts,
        elapsed=timedelta(seconds=elapsed),
        budget=budget.timeout,
        last=_last(observation),
    )


async def hold_stable(
    probe: Probe[T],
    *,
    invariant: Callable[[T], bool],
    transient: Classifier | None = None,
    budget: Budget,
    label: str,
) -> Outcome[T]:
    """Assert *invariant* holds for every reading across the whole budget.

    The negative assertion: success is the budget expiring with nothing having
    gone wrong. ``budget.timeout`` is therefore the hold *duration*, not a
    deadline to race — this function always spends it in full on the happy path.

    Args:
        probe: Takes one reading. Called once per poll.
        invariant: True while the reading is still acceptable. The first False
            ends the hold.
        transient: Classifies a probe exception, exactly as in
            :func:`poll_until`. Not on the signature the scaffold sketched, and
            added because the scenarios this exists for run out-of-cluster over
            a VPN plus a vcluster tunnel, where a reset connection mid-hold is
            routine: with no classifier the first blip ends a twenty-minute hold
            as :class:`~application_sdk.testing.harness.outcome.Indeterminate`.
            ``budget.max_transient_failures`` bounds the streak, as it does
            there.
        budget: The hold's allowance. ``start_grace`` and ``stall_timeout`` are
            not consulted: there is no start to wait for and no progress to
            watch.
        label: Noun phrase naming what must stay true ("worker replicas while
            the extract activity runs").

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` with the last
        reading when the invariant held throughout;
        :class:`~application_sdk.testing.harness.outcome.Stalled` — same
        "stopped being what it should be" shape — carrying the violating
        reading when it did not; or
        :class:`~application_sdk.testing.harness.outcome.Indeterminate` when the
        probe itself could not be read. A hold cannot pass over a window it
        never observed: "nothing went wrong" and "I did not look" are the same
        silence, which is the whole reason that third verdict exists.

    Raises:
        BaseException: Whatever ``probe`` raised, when ``transient`` does not
            classify it as worth absorbing.
    """
    interval_seconds = budget.poll_interval.total_seconds()
    retry_after_left = (budget.retry_after_budget or timedelta(0)).total_seconds()
    max_retry_after = _seconds(budget.max_retry_after)

    streak = 0
    attempts = 0
    elapsed = 0.0
    observation: _Observation[T] | None = None
    last_error: BaseException | None = None

    async for attempt in until_deadline_async(
        budget.timeout.total_seconds(),
        interval_seconds,
        label=label,
        heartbeat_seconds=(budget.heartbeat or timedelta(0)).total_seconds(),
    ):
        attempts = attempt.number
        elapsed = attempt.elapsed
        try:
            value = await probe()
        except Exception as error:
            backoff = transient(error) if transient is not None else None
            if backoff is None:
                raise
            last_error = error
            streak += 1
            if streak >= budget.max_transient_failures:
                logger.warning(
                    "giving up on the %s hold after %d consecutive probe "
                    "error(s) in %.0fs — the window went unobserved, so the "
                    "invariant is unproven rather than held",
                    label,
                    streak,
                    elapsed,
                    exc_info=True,
                )
                return Indeterminate(
                    label=label,
                    attempts=attempts,
                    elapsed=timedelta(seconds=elapsed),
                    cause=error,
                    transient_failures=streak,
                    last=_last(observation),
                )
            gap = _honoured_gap(
                backoff,
                floor=interval_seconds,
                cap=max_retry_after,
                budget_left=retry_after_left,
            )
            retry_after_left -= gap - interval_seconds
            attempt.sleep_next(gap)
            continue

        streak = 0
        observation = _Observation(value)
        if not invariant(value):
            return Stalled(
                label=label,
                attempts=attempts,
                elapsed=timedelta(seconds=elapsed),
                # How long it held before it broke — the other half of the
                # report line, and the reason a flap at 2s and a flap at 19m do
                # not read the same.
                stall_window=timedelta(seconds=elapsed),
                fingerprint=_render(value),
                last=value,
            )

    if observation is None:
        # Never read once across the whole hold — the *holder* is absent, which
        # a probe cannot produce. A hold over a `Probe[None]` whose every reading
        # was a legitimate `None` observed its window perfectly well, and tested
        # for with `is None` it would have been reported as unobservable.
        #
        # `observation is None` implies a classified error was absorbed: the loop
        # always yields one attempt, and an attempt either produced a reading or
        # raised. The fallback keeps `cause` typed rather than covering a path.
        return Indeterminate(
            label=label,
            attempts=attempts,
            elapsed=timedelta(seconds=elapsed),
            cause=last_error or RuntimeError(f"the {label} hold took no reading"),
            transient_failures=streak,
        )
    return Settled(
        label=label,
        attempts=attempts,
        elapsed=timedelta(seconds=elapsed),
        value=observation.value,
    )
