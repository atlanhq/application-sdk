"""Retry policies for App execution."""

from dataclasses import dataclass, field
from datetime import timedelta


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

    def with_max_attempts(self, max_attempts: int) -> "RetryPolicy":
        """Create a new policy with different max attempts."""
        return RetryPolicy(
            max_attempts=max_attempts,
            initial_interval=self.initial_interval,
            max_interval=self.max_interval,
            backoff_coefficient=self.backoff_coefficient,
            non_retryable_errors=self.non_retryable_errors,
        )

    def with_initial_interval(self, interval: timedelta) -> "RetryPolicy":
        """Create a new policy with different initial interval."""
        return RetryPolicy(
            max_attempts=self.max_attempts,
            initial_interval=interval,
            max_interval=self.max_interval,
            backoff_coefficient=self.backoff_coefficient,
            non_retryable_errors=self.non_retryable_errors,
        )

    def with_non_retryable(self, *error_types: type[Exception]) -> "RetryPolicy":
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
