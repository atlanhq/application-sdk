"""Framework-level execution errors.

Use these types in application code instead of importing directly from
``temporalio.exceptions``, which leaks the execution-backend dependency.
"""

from temporalio.exceptions import ApplicationError as _TemporalApplicationError


class ApplicationError(_TemporalApplicationError):
    """Framework application error.

    Raised to signal a non-fatal, optionally non-retryable workflow error.
    Prefer ``NonRetryableError`` (from ``application_sdk.app``) for errors that
    should abort a task without retrying — it provides a higher-level API.

    Use ``ApplicationError`` directly only when you need fine-grained control
    over Temporal's retry and error-type semantics.

    Args:
        message: Human-readable description of the error.
        *args: Additional positional arguments forwarded to Temporal.
        type: Optional string error type tag for categorisation.
        non_retryable: When ``True``, Temporal will not retry the activity.
        next_retry_delay: Override the next retry backoff interval.
        cause: Underlying exception that caused this error.
    """
