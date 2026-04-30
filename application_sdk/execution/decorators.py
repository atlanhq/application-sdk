from typing import Any, Callable, Optional

from application_sdk.constants import LOCK_METADATA_KEY
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def needs_lock(max_locks: int = 5, lock_name: Optional[str] = None):
    """Decorator to mark activities that require distributed locking.

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
