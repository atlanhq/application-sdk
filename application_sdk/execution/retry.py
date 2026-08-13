"""Retry policies for App execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from temporalio.common import RetryPolicy as _TemporalRetryPolicy


@dataclass(frozen=True)
class RetryPolicy:
    """Configuration for retry behavior.

    This is the framework's abstraction over retry configuration.
    The execution layer translates this to the underlying system's
    retry mechanism (e.g., Temporal's RetryPolicy).
    """

    max_attempts: int = 3
    """Maximum number of attempts (including the initial attempt)."""

    initial_interval: timedelta = field(default_factory=lambda: timedelta(seconds=1))
    """Initial delay between retries."""

    max_interval: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    """Maximum delay between retries."""

    backoff_coefficient: float = 2.0
    """Multiplier for exponential backoff."""

    non_retryable_errors: tuple[str, ...] = ()
    """Exception class names that should not be retried."""

    def with_max_attempts(self, max_attempts: int) -> RetryPolicy:
        """Create a new policy with different max attempts."""
        return RetryPolicy(
            max_attempts=max_attempts,
            initial_interval=self.initial_interval,
            max_interval=self.max_interval,
            backoff_coefficient=self.backoff_coefficient,
            non_retryable_errors=self.non_retryable_errors,
        )

    def with_initial_interval(self, interval: timedelta) -> RetryPolicy:
        """Create a new policy with different initial interval."""
        return RetryPolicy(
            max_attempts=self.max_attempts,
            initial_interval=interval,
            max_interval=self.max_interval,
            backoff_coefficient=self.backoff_coefficient,
            non_retryable_errors=self.non_retryable_errors,
        )

    def with_non_retryable(self, *error_types: type[Exception]) -> RetryPolicy:
        """Create a new policy with additional non-retryable errors."""
        error_names = tuple(e.__name__ for e in error_types)
        return RetryPolicy(
            max_attempts=self.max_attempts,
            initial_interval=self.initial_interval,
            max_interval=self.max_interval,
            backoff_coefficient=self.backoff_coefficient,
            non_retryable_errors=self.non_retryable_errors + error_names,
        )


# Common retry policies
NO_RETRY = RetryPolicy(max_attempts=1)
"""No retries - fail immediately on error."""

DEFAULT_RETRY = RetryPolicy()
"""Default retry policy (3 attempts, exponential backoff)."""

AGGRESSIVE_RETRY = RetryPolicy(
    max_attempts=10,
    initial_interval=timedelta(milliseconds=100),
    max_interval=timedelta(minutes=1),
    backoff_coefficient=1.5,
)
"""Aggressive retry policy for transient failures."""


#: Room a total-time ceiling leaves for retry backoff *between* attempts, in
#: seconds. Mirrors the ``+ 10`` in ``preflight_gate.gate_timeouts`` — same
#: reason: a ceiling of exactly ``attempts × attempt`` would fire during the
#: backoff wait before the last attempt could start, which silently costs an
#: attempt the retry policy says the task has.
_RETRY_BACKOFF_HEADROOM_SECONDS = 10


def retry_product_seconds(
    timeout_seconds: int,
    max_attempts: int,
    *,
    backoff_headroom_seconds: int = _RETRY_BACKOFF_HEADROOM_SECONDS,
) -> int:
    """The worst-case total seconds a task can consume across *every* attempt.

    The arithmetic a ``start_to_close`` timeout on its own hides (ADR-0018 →
    *Bounding total time*): a per-attempt ceiling multiplied by the retry
    budget. A ``StartToClose`` timeout is retryable in Temporal, so a wedged
    attempt really can spend the whole product — three attempts against a 24h
    backstop is a 72h worst case.

    Use it to declare a ``schedule_to_close_seconds`` that bounds the product at
    exactly what the retry policy already implies, without doing the
    multiplication by hand::

        @task(
            timeout_seconds=3600,
            retry_max_attempts=3,
            schedule_to_close_seconds=retry_product_seconds(3600, 3),
        )

    That is the *ceiling that changes nothing* — it makes today's worst case
    explicit and enforced. Passing anything smaller is a real bound, and
    :func:`resolve_activity_time_bounds` describes what it costs.

    What the headroom is, honestly: a fixed allowance, not a sum computed from
    the retry policy's shape. At the default backoff (1s initial interval, 2.0
    coefficient) it covers roughly 3–4 attempts' inter-attempt waits; a deeper
    policy (10 attempts at those settings waits ~17 minutes across its backoff,
    against 10s of headroom) can still fire the ceiling during a late backoff
    wait and forfeit the attempts the policy says remain. That is deliberate:
    the number stays policy-independent so it can be declared at decoration time,
    and slightly-tight is the right side to err on — the ceiling's job is to
    expose a cosmetic retry policy, not to reproduce its backoff arithmetic. If
    a task genuinely needs every deep-policy attempt, pass a larger
    ``backoff_headroom_seconds`` with it.

    Args:
        timeout_seconds: One attempt's ``start_to_close`` budget.
        max_attempts: The retry policy's ``max_attempts`` (attempts, not
            retries). Values below 1 are read as 1 — one attempt always runs.
        backoff_headroom_seconds: Seconds added for the retry backoff waits.

    Returns:
        Total seconds across all attempts, plus the backoff headroom described
        above.
    """
    return max(1, max_attempts) * timeout_seconds + backoff_headroom_seconds


def resolve_activity_time_bounds(
    timeout_seconds: int, schedule_to_close_seconds: int | None
) -> tuple[int, int | None]:
    """Resolve one task's ``(start_to_close, schedule_to_close)`` pair.

    The single reader of ``TaskMetadata.schedule_to_close_seconds``, shared by
    both dispatch paths (``app.base._create_task_activity_wrapper`` and
    ``activities.get_activity_options``) so the pair they hand Temporal cannot
    diverge.

    Two resolutions, and neither is a guess:

    - ``None`` — the SDK's default and today's behaviour: one attempt is
      bounded, the *product* is not. Nothing is sent for
      ``schedule_to_close_timeout``.
    - A ceiling **below** one attempt's timeout wins over that timeout, and
      caps it. This is the shape an app gets by declaring only a total
      ("this task must finish within an hour") while inheriting a generous
      per-attempt backstop from ``ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS``, and
      honouring the tighter number is what the author asked for. Capping here
      rather than sending an inverted pair keeps the two timeouts consistent
      whatever the server does with an inversion, and means one attempt gets
      the whole ceiling — a retry could not start inside it anyway.

    Note the ceiling bounds one *dispatch*, which is what the retry policy
    spends. Worker-eviction re-dispatches (``eviction_retry``) each get a fresh
    window by design — they are bounded separately by
    :data:`~application_sdk.constants.WORKER_EVICTION_MAX_RETRIES`, and
    deliberately do not spend the application-error retry budget.

    Args:
        timeout_seconds: The task's per-attempt ``start_to_close`` budget.
        schedule_to_close_seconds: The task's declared total ceiling, or
            ``None`` to leave the product unbounded.

    Returns:
        ``(start_to_close_seconds, schedule_to_close_seconds_or_None)``.
    """
    if schedule_to_close_seconds is None:
        return timeout_seconds, None
    return min(timeout_seconds, schedule_to_close_seconds), schedule_to_close_seconds


def _with_worker_evicted_non_retryable(non_retryable: list[str]) -> list[str]:
    """Append ``WORKER_EVICTED_TYPE`` to a non-retryable-types list, idempotently.

    The SDK enforces that Temporal never auto-retries activities terminated by
    worker pod eviction: the workflow-side eviction loop owns that retry
    decision and re-dispatches the activity as a fresh attempt without burning
    the application-error retry budget. Both code paths that build a Temporal
    ``RetryPolicy`` (``_to_temporal_retry_policy`` here and
    ``activities.get_activity_options``) route through this helper to keep that
    invariant in one place.
    """
    from application_sdk.errors.leaves import (  # noqa: PLC0415 — keep retry.py free of eager imports of error hierarchy
        WORKER_EVICTED_TYPE,
    )

    result = list(non_retryable)
    if WORKER_EVICTED_TYPE not in result:
        result.append(WORKER_EVICTED_TYPE)
    return result


def _to_temporal_retry_policy(policy: RetryPolicy) -> _TemporalRetryPolicy:
    """Convert a framework :class:`RetryPolicy` to ``temporalio.common.RetryPolicy``.

    The SDK always appends ``WorkerEvicted`` to ``non_retryable_error_types``
    via :func:`_with_worker_evicted_non_retryable`.

    Internal helper for the execution layer.  Not part of the public API.
    """
    from temporalio.common import (  # noqa: PLC0415 — cold path: only when constructing temporal retry policy
        RetryPolicy as _TR,
    )

    return _TR(
        maximum_attempts=policy.max_attempts,
        initial_interval=policy.initial_interval,
        maximum_interval=policy.max_interval,
        backoff_coefficient=policy.backoff_coefficient,
        non_retryable_error_types=_with_worker_evicted_non_retryable(
            list(policy.non_retryable_errors)
        ),
    )
