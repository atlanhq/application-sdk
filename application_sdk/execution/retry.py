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
    from application_sdk.app.base import (  # noqa: PLC0415 — circular: app.base imports execution.retry transitively
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
