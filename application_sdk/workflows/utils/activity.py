"""
Note:
- We have activities that can run for a long time, in case of a failure (say: worker crash)
  Temporal will not retry the activity until the configured timeout is reached.
- We add auto_heartbeater to activities to ensure an failure is detected earlier
  and the activity is retried.

source:
- https://temporal.io/blog/activity-timeouts
- https://github.com/temporalio/samples-python/blob/main/custom_decorator/activity_utils.py
"""

import asyncio
from datetime import timedelta
from functools import wraps
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast

from temporalio import activity

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

"""
Note:
- We have activities that can run for a long time, in case of a failure (say: worker crash)
  Temporal will not retry the activity until the configured timeout is reached.
- We add auto_heartbeater to activities to ensure an failure is detected earlier
  and the activity is retried.

source:
- https://temporal.io/blog/activity-timeouts
- https://github.com/temporalio/samples-python/blob/main/custom_decorator/activity_utils.py
"""

import asyncio
from datetime import timedelta
from functools import wraps
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast

from temporalio import activity

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def auto_heartbeater(fn: F) -> F:
    """
    Auto-heartbeater for activities.

    :param fn: The activity function.
    :return: The activity function.

    Usage:
        >>> @activity.defn
        >>> @auto_heartbeater
        >>> async def my_activity():
        >>>     pass
    """

    # We want to ensure that the type hints from the original callable are
    # available via our wrapper, so we use the functools wraps decorator
    @wraps(fn)
    async def wrapper(*args, **kwargs):
        heartbeat_timeout: Optional[timedelta] = None

        # Default to 2 minutes if no heartbeat timeout is set
        default_heartbeat_timeout = timedelta(seconds=120)
        try:
            activity_heartbeat_timeout = activity.info().heartbeat_timeout
            heartbeat_timeout = (
                activity_heartbeat_timeout
                if activity_heartbeat_timeout
                else default_heartbeat_timeout
            )
        except RuntimeError:
            heartbeat_timeout = default_heartbeat_timeout

        # Heartbeat thrice as often as the timeout
        heartbeat_task = asyncio.create_task(
            heartbeat_every(heartbeat_timeout.total_seconds() / 3)
        )
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            print(f"Error in activity: {e}")
            raise e
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                # Wait for heartbeat cancellation to complete
                await asyncio.wait([heartbeat_task])

    return cast(F, wrapper)


async def heartbeat_every(delay: float, *details: Any) -> None:
    """
    Heartbeat every so often while not cancelled

    :param delay: The delay between heartbeats.
    :param details: The details to heartbeat.
    """
    # Heartbeat every so often while not cancelled
    while True:
        await asyncio.sleep(delay)
        activity.heartbeat(*details)
