from typing import Callable, Optional

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

LOCK_METADATA_KEY = "__lock_metadata__"


def needs_lock(max_locks: int = 5, lock_name: Optional[str] = None):
    """Decorator to mark activities that require distributed locking.

    This decorator attaches lock configuration directly to the activity
    definition that will be used by the workflow interceptor to acquire
    locks before executing activities.

    Args:
        max_locks: Maximum number of concurrent locks allowed
        lock_name: Optional custom name for the lock (defaults to activity name)

    Example:
        ```python
        @activity.defn
        @needs_lock(max_locks=3)
        async def my_activity(ctx, input: str) -> str:
            return process_with_lock(input)
        ```
    """

    def decorator(func: Callable) -> Callable:
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
