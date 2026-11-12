from typing import Any, Callable, Optional

from typing_extensions import deprecated

# The public name in application_sdk.constants is a deprecated alias.
from application_sdk.constants import _LOCK_METADATA_KEY as LOCK_METADATA_KEY
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@deprecated(
    "needs_lock has no effect: create_worker registers neither the lock "
    "interceptor nor the lock activities, so a decorated task runs with no "
    "concurrency limit. Remove the decorator; it will be removed in v4.0.0."
)
def needs_lock(max_locks: int = 5, lock_name: Optional[str] = None):
    """Deprecated and has no effect: ``create_worker`` registers no lock interceptor — removed in v4.0.0.

    .. deprecated:: 3.57.0
        Has no effect. It only records ``__lock_metadata__`` on the function,
        and nothing in v3 reads it: ``create_worker`` registers neither the lock
        interceptor nor the lock activities. Remove the decorator. Will be
        removed in v4.0.0.

    This decorator attaches lock configuration directly to the activity
    definition that will be used by the workflow interceptor to acquire
    locks before executing activities.

    Note:
        Activities decorated with ``needs_lock`` must be called with
        ``schedule_to_close_timeout`` to ensure proper lock TTL calculation
        that covers retries.

    Args:
        max_locks (int): Maximum number of concurrent locks allowed.
        lock_name (str | None): Optional custom name for the lock (defaults to activity name).

    Raises:
        WorkflowError: If activity is called without ``schedule_to_close_timeout``.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        # Store lock metadata directly on the function object
        metadata = {
            "is_needs_lock": True,
            "max_locks": max_locks,
            "lock_name": lock_name or func.__name__,
        }

        # Attach metadata to the function
        setattr(func, LOCK_METADATA_KEY, metadata)

        return func

    return decorator
