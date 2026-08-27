"""The deadline arithmetic every bounded loop in the harness runs on.

Every "probe until it's ready or the budget runs out" loop routes through
:func:`until_deadline` (sync) or :func:`until_deadline_async` (async). Before
this module each of them hand-rolled the same three lines::

    deadline = time.monotonic() + timeout
    while True:
        ...work...
        if time.monotonic() >= deadline:
            raise ...
        time.sleep(interval)

which is wrong in a way that is easy to miss: the budget check sits *after* the
work, so the loop sleeps one whole interval past its stated timeout before
noticing. :attr:`Attempt.is_last` moves that decision to before the sleep — the
call site is told "this is your last look" and the loop stops there.

The primitive is a **generator, not a callback**, so each call site keeps its
own ``try``/``except`` and raises its own typed error leaf on exhaustion::

    for attempt in until_deadline(timeout_s, interval_s, label="worker health"):
        if probe_ok(url):
            return
        if attempt.is_last:
            raise WorkerNotHealthyError(
                attempts=attempt.number, elapsed_seconds=attempt.elapsed
            )

What the loop owns: the clock, the sleeping, the off-by-one (including the gap
after a probe that overran its own interval), and a throttled
heartbeat log so a multi-minute wait doesn't look wedged in CI output. What the
call site owns: the probe, the success condition, and the failure.

Testing: pass ``clock``/``sleep`` explicitly, or wrap the code under test in
:func:`fake_clock`. Neither touches :func:`time.monotonic` itself — the asyncio
event loop reads that for its own timers, and fast-forwarding it globally makes
async tests flake.

Private, and it lives here rather than in ``testing/e2e/`` because the harness
cannot import from the package it is about to be the foundation of: child H
re-expresses ``testing/e2e`` *over*
:mod:`application_sdk.testing.harness.waiting`, and a
``harness -> e2e -> harness`` cycle is what keeping the deadline loop on the e2e
side would have produced. Both sides read it from here today: child C moved it
and rewired five ``testing/e2e`` call sites byte for byte, and child D (FND-240)
brought the rest across — after which **no bounded loop in the harness owns a
deadline**, and there is one clock rather than the monotonic-deadline /
elapsed-accumulator / ``time.time()`` three that coexisted before.

The remaining direct callers are the ones whose probe is *synchronous* — a
connector-supplied write, a local test client — and so cannot reach
:func:`~application_sdk.testing.harness.waiting.poll_until` without blocking the
bridge's loop inside someone else's code. Everything async goes through the
primitive instead, and gets the guards and the verdict vocabulary with it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Cadence for the "still waiting" heartbeat line. Long enough not to drown the
# log on a fast happy-path run, short enough that an operator watching a CI leg
# can tell "still polling" from "harness wedged". Also reused by
# ``client.poll_native_status``, which throttles its own richer progress line to
# the same cadence.
_HEARTBEAT_SECONDS = 30.0

# Module-level indirection for the defaults, so :func:`fake_clock` can swap them
# without reassigning ``time.monotonic`` / ``time.sleep`` process-wide. Read at
# call time, never captured as a default argument value.
_monotonic: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep
_async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass
class Attempt:
    """One poll of a bounded-deadline loop.

    Attributes:
        number: 1-based attempt count. Feeds an ``attempts=`` error field
            directly instead of being re-derived at the call site.
        elapsed: Seconds since the loop started, as measured by the loop's own
            clock. Feeds an ``elapsed_seconds=`` error field.
        remaining: Seconds left in the budget (never negative).
        is_last: ``True`` on the final attempt the loop will yield — there is
            not enough budget left for another interval. A call site that has
            not succeeded by now must raise here; the loop will not come back.
    """

    number: int
    elapsed: float
    remaining: float
    is_last: bool
    # Set by ``sleep_next``; read by the loop after the yield resumes.
    _gap: float | None = field(default=None, repr=False, compare=False)

    def sleep_next(self, seconds: float) -> float:
        """Wait ``seconds`` before the next poll instead of the fixed interval.

        For call sites that honour an origin-supplied backoff (an HTTP
        ``retry_after``) in place of their own cadence.

        Args:
            seconds: The gap the origin asked for.

        Returns:
            The gap the loop will wait — clamped to :attr:`remaining`, so a 120s
            request against a 50s residual budget cannot push the loop past its
            own deadline. Log this, not the requested value. Measured against the
            budget as of *this* attempt; a probe that then overruns its own
            interval shortens the real gap further (see :func:`_next_gap`).

        Note:
            A no-op on an :attr:`is_last` attempt: the loop stops rather than
            sleeping, so there is no next poll to delay.
        """
        gap = min(max(seconds, 0.0), self.remaining)
        self._gap = gap
        return gap


def monotonic() -> float:
    """Read the clock the bounded-wait loops run on.

    For code that keeps its own elapsed-time ledger *alongside* a loop rather
    than inside one — a probe wrapper that logs progress, or one that has to
    report how long a reading has been unchanged. Such a wrapper is called by
    :func:`~application_sdk.testing.harness.waiting.poll_until`, which does not
    hand it the :class:`Attempt`, so the only way for the two to agree on
    "elapsed" is to read the same clock.

    Reading it through this function rather than calling :func:`time.monotonic`
    directly is what puts the wrapper under :func:`fake_clock` with the loop it
    accompanies. A wrapper on the real clock inside a fake-clock test reports
    zero elapsed for a wait the loop believes ran for ten minutes, which is
    exactly the sort of disagreement a progress ledger exists to rule out.
    """
    return _monotonic()


async def sleep_async(seconds: float) -> None:
    """Await *seconds* through this module's swappable sleep.

    The gap between two attempts of a *retry* loop rather than of a deadline
    loop — a bounded ``for`` over :func:`until_deadline_async` owns its own
    sleeping, but the AE write retries in
    :mod:`application_sdk.testing.harness.automation_engine.client` count
    attempts instead of watching a clock, so they have no :class:`Attempt` to
    ask. Routing them here rather than calling :func:`asyncio.sleep` directly
    puts them under :func:`fake_clock` with everything else, so a test asserts a
    retry's real gap sequence instead of counting calls to a patched sleep.
    """
    await _async_sleep(seconds)


def _log_heartbeat(label: str, attempt: Attempt, timeout_seconds: float) -> None:
    """Emit the throttled "still waiting" progress line.

    Deliberately a function rather than an inline statement: the log is throttled
    to ``heartbeat_seconds`` by its caller, so it is not per-iteration volume and
    should not read as an in-loop INFO.
    """
    logger.info(
        "Still waiting on %s: attempt %d, %.0fs of %.0fs elapsed (%.0fs left)",
        label,
        attempt.number,
        attempt.elapsed,
        timeout_seconds,
        attempt.remaining,
    )


def _next_attempt(
    number: int,
    start: float,
    timeout_seconds: float,
    interval_seconds: float,
    now: float,
) -> Attempt:
    """Build the attempt for the current clock reading."""
    elapsed = now - start
    remaining = max(timeout_seconds - elapsed, 0.0)
    return Attempt(
        number=number,
        elapsed=elapsed,
        remaining=remaining,
        # No room for another interval means no further attempt: stop here
        # rather than sleeping past the stated budget to find out.
        is_last=remaining <= interval_seconds,
    )


def _next_gap(
    attempt: Attempt,
    start: float,
    timeout_seconds: float,
    interval_seconds: float,
    now: float,
) -> float:
    """How long to wait before the next poll, given the clock after the probe.

    ``attempt.remaining`` was measured *before* the probe ran. A probe that
    outlasts its own interval has already eaten into the budget, so sleeping the
    full gap on top of it would carry the loop past the deadline — the exact
    overshoot ``is_last`` exists to prevent, arriving by a different route. Read
    the clock again and clamp.

    Clamping to zero costs the call site nothing: the next iteration still yields
    a final attempt with ``is_last=True``, so its exhaustion branch always fires.
    """
    requested = attempt._gap if attempt._gap is not None else interval_seconds
    return min(requested, max(timeout_seconds - (now - start), 0.0))


def until_deadline(
    timeout_seconds: float,
    interval_seconds: float,
    *,
    label: str,
    heartbeat_seconds: float = _HEARTBEAT_SECONDS,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Iterator[Attempt]:
    """Yield one :class:`Attempt` per poll until the budget expires, sleeping between.

    The final attempt is the last one for which a further ``interval_seconds``
    still fits inside ``timeout_seconds``, and each gap is re-clamped against the
    clock read *after* the probe returns, so the loop never sleeps beyond its own
    deadline — not even when a probe outlasts its own interval.

    At least one attempt is always yielded: a zero or negative budget still gets a
    single probe, carrying ``is_last=True``. This is a deliberate contract change
    from the hand-rolled ``while clock() < deadline`` loops this replaced, which
    probed zero times on a non-positive budget. Exhaustion branches key on
    ``is_last``, so a loop that yields nothing raises nothing — the failure would
    vanish silently instead of surfacing as a typed error. No call site passes a
    non-positive budget today (every one reads a positive ``ClassVar`` default),
    so the change is contract-only; a caller that genuinely wants
    zero-budget-means-no-work must check that before entering the loop.

    Args:
        timeout_seconds: Total wall-clock budget for the whole loop.
        interval_seconds: Gap between polls. Pass ``0`` to spin.
        label: What is being waited on, for the heartbeat line. A noun phrase
            naming the concrete target ("worker health at <url>"), not a verb.
        heartbeat_seconds: Cadence of the "still waiting" INFO line. ``0``
            disables it — use that when the call site logs its own per-poll
            progress and a second line would be duplicate noise.
        clock: Monotonic time source. Defaults to :func:`time.monotonic`.
        sleep: Blocking sleep. Defaults to :func:`time.sleep`.

    Yields:
        One :class:`Attempt` per poll, in order.
    """
    read_clock = clock or _monotonic
    do_sleep = sleep or _sleep
    start = read_clock()
    number = 0
    last_heartbeat = 0.0
    while True:
        number += 1
        attempt = _next_attempt(
            number, start, timeout_seconds, interval_seconds, read_clock()
        )
        if (
            heartbeat_seconds > 0
            and (attempt.elapsed - last_heartbeat) >= heartbeat_seconds
        ):
            last_heartbeat = attempt.elapsed
            _log_heartbeat(label, attempt, timeout_seconds)
        yield attempt
        if attempt.is_last:
            return
        do_sleep(
            _next_gap(attempt, start, timeout_seconds, interval_seconds, read_clock())
        )


async def until_deadline_async(
    timeout_seconds: float,
    interval_seconds: float,
    *,
    label: str,
    heartbeat_seconds: float = _HEARTBEAT_SECONDS,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> AsyncIterator[Attempt]:
    """Async twin of :func:`until_deadline` — identical semantics, awaits the sleep.

    A single generator cannot straddle both worlds: ``async for`` needs an async
    generator, and awaiting inside a sync one is not possible. The attempt
    arithmetic is shared via :func:`_next_attempt` so the two cannot drift.

    Args:
        timeout_seconds: Total wall-clock budget for the whole loop.
        interval_seconds: Gap between polls.
        label: What is being waited on, for the heartbeat line.
        heartbeat_seconds: Cadence of the "still waiting" INFO line; ``0`` off.
        clock: Monotonic time source. Defaults to :func:`time.monotonic`.
        sleep: Awaitable sleep. Defaults to :func:`asyncio.sleep`.

    Yields:
        One :class:`Attempt` per poll, in order.
    """
    read_clock = clock or _monotonic
    do_sleep = sleep or _async_sleep
    start = read_clock()
    number = 0
    last_heartbeat = 0.0
    while True:
        number += 1
        attempt = _next_attempt(
            number, start, timeout_seconds, interval_seconds, read_clock()
        )
        if (
            heartbeat_seconds > 0
            and (attempt.elapsed - last_heartbeat) >= heartbeat_seconds
        ):
            last_heartbeat = attempt.elapsed
            _log_heartbeat(label, attempt, timeout_seconds)
        yield attempt
        if attempt.is_last:
            return
        await do_sleep(
            _next_gap(attempt, start, timeout_seconds, interval_seconds, read_clock())
        )


# ---------------------------------------------------------------------------
# Test seam
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """Deterministic stand-in for ``time.monotonic`` + the sleeps.

    :meth:`sleep` advances :attr:`now` instead of blocking, so a poll loop under
    this clock runs its whole budget instantly and in a fixed, assertable number
    of iterations.

    Attributes:
        now: Current monotonic reading, advanced by every sleep.
        slept: Every gap slept, in order — the assertion target for "did the
            loop honour the backoff and clamp it to the remaining budget?".
    """

    now: float = 0.0
    slept: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        """Return the current reading without advancing it."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record the gap and fast-forward :attr:`now` by it."""
        self.slept.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        """Awaitable :meth:`sleep`, for :func:`until_deadline_async`."""
        self.sleep(seconds)


@contextmanager
def fake_clock(start: float = 0.0) -> Iterator[FakeClock]:
    """Swap the poll helpers' default clock and sleeps for a deterministic fake.

    For testing code that polls but does not expose ``clock``/``sleep`` on its
    own signature. Only this module's defaults are swapped — ``time.monotonic``
    and ``time.sleep`` are untouched, so nothing else in the process sees a
    fast-forwarded clock. That matters for the asyncio event loop, which reads
    ``time.monotonic`` for its own timers: patching it globally makes async tests
    flake with spurious ``StopIteration``.

    Args:
        start: Initial monotonic reading.

    Yields:
        The :class:`FakeClock` in force for the duration of the block.
    """
    global _monotonic, _sleep, _async_sleep  # noqa: PLW0603 — the swap is the point
    fake = FakeClock(now=start)
    saved = (_monotonic, _sleep, _async_sleep)
    _monotonic, _sleep, _async_sleep = fake.monotonic, fake.sleep, fake.async_sleep
    try:
        yield fake
    finally:
        _monotonic, _sleep, _async_sleep = saved
