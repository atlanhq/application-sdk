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

The clock, the sleeping and the off-by-one already live in
:mod:`application_sdk.testing.e2e._poll` (``until_deadline`` /
``until_deadline_async``, monotonic, with the gap re-clamped against the clock
read *after* the probe). :func:`poll_until` is built on that rather than on a
second deadline implementation; the private module moves here as part of child C.

:func:`hold_stable` is **new**. All twelve bounded loops in ``testing/e2e/``
today are "wait until"; not one is "assert stays". The negative assertion is
what the runtime scaling scenarios turn on — "a busy pod is never scaled away",
"it must not scale down while a long activity is still running", and the whole
steady-state group — because most scaling bugs are wrong *actions* rather than
wrong states.

Implementation is FND-227 (child C).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeAlias, TypeVar

from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import Outcome

__all__ = ["Probe", "hold_stable", "poll_until"]

T = TypeVar("T")

#: One reading of whatever is being waited on. Async by construction (decision
#: D1): there is no synchronous probe anywhere in the harness.
Probe: TypeAlias = Callable[[], Awaitable[T]]


async def poll_until(
    probe: Probe[T],
    *,
    settled: Callable[[T], bool],
    started: Callable[[T], bool] | None = None,
    fingerprint: Callable[[T], str] | None = None,
    transient: Callable[[BaseException], timedelta | None] | None = None,
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
        fingerprint: Reduces a reading to a comparable progress string. When it
            stops changing for ``budget.stall_timeout`` the wait returns
            :class:`~application_sdk.testing.harness.outcome.Stalled` carrying
            the frozen fingerprint. ``None`` disables the watchdog regardless of
            the budget, because there is nothing to compare.
        transient: Classifies a probe exception. Returns a backoff to honour
            (bounded by ``budget.retry_after_budget``) to absorb it as
            transient, or ``None`` to treat it as terminal. An unclassified
            exception propagates: a deterministic bug in the probe itself — a
            wrong call signature, a bad config value — raises the same exception
            on every attempt, so waiting out the budget only delays the failure.
        budget: The wait's whole allowance. See
            :class:`~application_sdk.testing.harness.budgets.Budget`.
        label: Noun phrase naming the concrete target, for the report and the
            heartbeat line. Written for whoever reads a red CI leg.

    Returns:
        The verdict. Never raises on a failed wait — see
        :mod:`application_sdk.testing.harness.outcome` for why, and
        :func:`~application_sdk.testing.harness.outcome.assert_settled` for the
        raise-on-failure adapter.

    Raises:
        NotImplementedError: Always — implementation is FND-227 (child C).
    """
    raise NotImplementedError("poll_until is FND-227 (child C)")


async def hold_stable(
    probe: Probe[T],
    *,
    invariant: Callable[[T], bool],
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
        probe itself could not be read.

    Raises:
        NotImplementedError: Always — implementation is FND-227 (child C).
    """
    raise NotImplementedError("hold_stable is FND-227 (child C)")
