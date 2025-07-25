"""
Lock management module for distributed task execution.

This module provides decorators for marking activities that require distributed
locking using Dapr's lock building block. The lock configuration is stored
in environment variables for use by interceptors.
"""

import json
import os
from functools import wraps
from typing import Any, Callable

from application_sdk.constants import APPLICATION_NAME, LOCK_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def needs_lock(max_slots: int = 5, ttl_seconds: int = 50):
    """Marks an activity as requiring distributed lock management.

    This decorator stores lock configuration in environment variables
    for use by workflow interceptors. The actual lock management is
    handled by the interceptor at runtime.

    Args:
        max_slots: int, maximum number of concurrent executions allowed (default: 5)
        ttl_seconds: int, time-to-live for locks in seconds (default: 50)

    Returns:
        Callable: Decorated function that can be intercepted for lock management

    Example:
        ```python
        @activity.defn
        @needs_lock(max_slots=3, ttl_seconds=30)
        async def my_activity(ctx: activity.Context, input: str) -> str:
            return await process_with_lock(input)
        ```

    Note:
        - This decorator only marks the activity for lock management
        - Actual lock acquisition and release is handled by workflow interceptors
        - Lock configuration is stored in environment variables
        - Uses Dapr's lock building block with Redis backend
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            return await func(*args, **kwargs)

        # Store lock configuration for interceptor
        os.environ["lock_config"] = json.dumps(
            {"needs_lock": True, "max_slots": max_slots, "ttl": ttl_seconds}
        )
        return wrapper

    return decorator
