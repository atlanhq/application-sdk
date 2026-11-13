"""One backoff policy and one async retry loop for every in-process retry.

The SDK used to carry about a dozen hand-rolled retry ladders, with three
jitter conventions between them and five with no jitter at all. A fleet of
workers that all lose the same dependency at once then retries in lockstep,
which is the thundering herd the jitter exists to break up. Each ladder now
states its shape as a :class:`Backoff`, and the loop that walks one lives in
:func:`retry_async` only.

Two shapes cover every site:

* **Geometric:** ``base * factor ** (retry - 1)``, clamped to ``cap``. Used by
  the Temporal connect and worker-restart ladders and the Dapr cold-start
  budget.
* **Explicit steps:** a literal tuple of delays, for a ladder an operator
  reads as a list (``(5, 10, 20)``). The tuple sets the number of retries.

Jitter is applied on top of the nominal delay:

* :attr:`Jitter.FULL` draws from ``U(0, nominal)`` (the AWS "full jitter"
  scheme). This is the default, because it spreads concurrent retries the
  widest.
* :attr:`Jitter.SPREAD` scales by ``U(0.5, 1.5)``. The mean stays at the
  nominal delay, for ladders whose floor matters (a gateway that needs several
  seconds to sync its routes).
* :attr:`Jitter.NONE` is for loops that must be reproducible, above all
  anything running on the workflow path, where randomness breaks replay.

A loop with its own control flow (a supervisor that resets its streak, or one
racing a shutdown event) keeps its loop and calls :meth:`Backoff.delay` for the
number. The arithmetic and the jitter are what must not drift.

Retries the SDK does not own, kept as library configuration on purpose:

====================================================  ==========================
Site                                                  Owner
====================================================  ==========================
``clients/base.py`` connection setup                  httpcore ``retries=`` (no
                                                      request bytes sent, so
                                                      safe for POST)
``infrastructure/_dapr/http.py`` transport            ``httpx-retries``
                                                      ``Retry`` (jittered
                                                      ``2**n`` ladder)
``common/aws_utils.py`` STS / boto calls              botocore retry config
``_obstore_config.py`` object-store client            object_store's Rust retry
                                                      (5xx / 429 / 408)
Temporal activity and workflow retries                ``RetryPolicy`` (server
                                                      side)
====================================================  ==========================
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

_T = TypeVar("_T")

#: Exponent clamp. A raised attempt count or budget must never compute a huge
#: power of two just to have the cap throw it away (``2**1100`` overflows a
#: float).
_MAX_EXPONENT = 30


class Jitter(Enum):
    """How a nominal delay is randomised. See the module docstring."""

    NONE = "none"
    FULL = "full"
    SPREAD = "spread"


#: Bounds of the :attr:`Jitter.SPREAD` multiplier.
SPREAD_BOUNDS: tuple[float, float] = (0.5, 1.5)


@dataclass(frozen=True, slots=True)
class Backoff:
    """The shape of one retry ladder.

    Attributes:
        base: First nominal delay, in seconds (geometric shape).
        factor: Growth per retry (geometric shape).
        cap: Upper bound on any single nominal delay, or ``None``.
        steps: Explicit nominal delays. When non-empty, this replaces
            ``base``/``factor``, and ``len(steps)`` is the number of retries.
        jitter: How each nominal delay is randomised.
        max_attempts: Total calls :func:`retry_async` makes, the first one
            included. ``None`` means no count limit.
        budget_seconds: Wall-clock budget for :func:`retry_async`, measured
            when an attempt fails. A delay is clamped to what remains, so the
            last retry can start right at the deadline: that final attempt is
            deliberate (a dependency that comes back at the deadline still
            gets one try), which makes the budget a bound on *waiting*, not on
            total runtime. No retry is scheduled once a failure lands past it.
            ``None`` means no time limit.
    """

    base: float = 0.0
    factor: float = 2.0
    cap: float | None = None
    steps: tuple[float, ...] = ()
    jitter: Jitter = Jitter.FULL
    max_attempts: int | None = None
    budget_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base < 0 or self.factor < 0 or any(s < 0 for s in self.steps):
            raise ValueError("backoff delays and factor must be non-negative")
        if self.cap is not None and self.cap < 0:
            raise ValueError(f"cap must be non-negative, got {self.cap}")

    @property
    def retries(self) -> int | None:
        """Retries after the first call, or ``None`` when only time-bounded."""
        limits = [n - 1 for n in (self.max_attempts,) if n is not None]
        if self.steps:
            limits.append(len(self.steps))
        return min(limits) if limits else None

    def nominal(self, retry: int) -> float:
        """The un-jittered delay before retry number *retry* (1-based)."""
        if retry < 1:
            raise ValueError(f"retry is 1-based, got {retry}")
        if self.steps:
            delay = self.steps[min(retry, len(self.steps)) - 1]
        else:
            delay = self.base * self.factor ** min(retry - 1, _MAX_EXPONENT)
        return delay if self.cap is None else min(delay, self.cap)

    def delay(self, retry: int) -> float:
        """The jittered delay before retry number *retry* (1-based)."""
        nominal = self.nominal(retry)
        if self.jitter is Jitter.FULL:
            return random.uniform(0.0, nominal)  # noqa: S311 — backoff spread, not crypto
        if self.jitter is Jitter.SPREAD:
            return nominal * random.uniform(*SPREAD_BOUNDS)  # noqa: S311 — backoff spread, not crypto
        return nominal


@dataclass(frozen=True, slots=True)
class RetryExhausted:
    """What :func:`retry_async` knows when it stops retrying.

    Handed to ``on_giveup`` so a caller can raise a terminal error that names
    the attempt count and the elapsed time, instead of re-raising the last
    transient one.
    """

    error: Exception
    attempts: int
    elapsed_seconds: float


async def retry_async(
    call: Callable[[], Awaitable[_T]],
    *,
    backoff: Backoff,
    retry_if: Callable[[Exception], bool],
    on_retry: Callable[[int, Exception, float], None] | None = None,
    on_giveup: Callable[[RetryExhausted], Exception | None] | None = None,
    sleep: Callable[[float], Awaitable[object]] | None = None,
    clock: Callable[[], float] | None = None,
) -> _T:
    """Call *call* until it succeeds, *retry_if* rejects its error, or
    *backoff* runs out.

    Args:
        call: Zero-argument coroutine factory. It must be idempotent, because
            it may run more than once.
        backoff: Ladder shape and limits. It must set ``steps``,
            ``max_attempts`` or ``budget_seconds``, because an unbounded retry
            belongs in a supervisor loop that calls :meth:`Backoff.delay`
            itself.
        retry_if: ``True`` when an exception is worth another attempt. Every
            other exception propagates at once, unchanged.
        on_retry: Called as ``(attempt, error, delay)`` before each sleep.
            Callers log here, so each site keeps its own redaction rules.
        on_giveup: Called once when a retryable error exhausts *backoff*. A
            returned exception is raised ``from`` the last error. ``None`` (or
            no hook) re-raises the last error itself.
        sleep: Awaitable sleep. Defaults to :func:`asyncio.sleep` looked up
            at call time.
        clock: Monotonic clock for ``budget_seconds``. Defaults to
            :func:`time.monotonic` looked up at call time.

    Returns:
        Whatever *call* returns.
    """
    retries = backoff.retries
    if retries is None and backoff.budget_seconds is None:
        raise ValueError(
            "retry_async needs a bounded Backoff (steps, max_attempts or budget_seconds)"
        )
    do_sleep = sleep if sleep is not None else asyncio.sleep
    now = clock if clock is not None else time.monotonic
    started = now()
    attempt = 0
    while True:
        attempt += 1
        try:
            return await call()
        # conformance: ignore[E004] retry classifier; anything retry_if rejects is re-raised unchanged
        except Exception as exc:
            if not retry_if(exc):
                raise
            elapsed = now() - started
            delay = backoff.delay(attempt)
            out_of_retries = retries is not None and attempt > retries
            if backoff.budget_seconds is not None:
                remaining = backoff.budget_seconds - elapsed
                out_of_retries = out_of_retries or remaining <= 0
                delay = min(delay, max(remaining, 0.0))
            if out_of_retries:
                terminal = (
                    on_giveup(RetryExhausted(exc, attempt, elapsed))
                    if on_giveup is not None
                    else None
                )
                if terminal is None:
                    raise
                raise terminal from exc
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await do_sleep(delay)
