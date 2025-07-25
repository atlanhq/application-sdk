import json
import os
from typing import Callable, Optional

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def needs_lock(max_locks: int = 5, lock_name: Optional[str] = None):
    """Decorator to mark activities that require distributed locking.

    This decorator stores lock configuration that will be used by the workflow interceptor
    to acquire locks before executing activities.

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
        os.environ["lock_config"] = json.dumps(
            {
                "needs_lock": True,
                "max_locks": max_locks,
                "activity_name": func.__name__,
                "lock_name": lock_name,
            }
        )
        return func

    return decorator
