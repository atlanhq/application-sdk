"""Utility functions for Temporal activities.

This module provides utility functions for working with Temporal activities,
including workflow ID retrieval, automatic heartbeating, and periodic heartbeat sending.
"""

import asyncio
from datetime import timedelta
from functools import wraps
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast

from temporalio import activity

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def get_workflow_id() -> str:
    """Get the workflow ID from the current activity.

    Retrieves the workflow ID from the current activity's context. This function
    must be called from within an activity execution context.

    Returns:
        The workflow ID of the current activity.

    Raises:
        RuntimeError: If called outside of an activity context.
        Exception: If there is an error retrieving the workflow ID.

    Example:
        >>> workflow_id = get_workflow_id()
        >>> print(workflow_id)  # e.g. "my-workflow-123"
    """
    try:
        return activity.info().workflow_id
    except Exception as e:
        logger.error("Failed to get workflow id", exc_info=e)
        raise Exception("Failed to get workflow id")


def auto_heartbeater(fn: F) -> F:
    """Decorator that automatically sends heartbeats during activity execution.

    Heartbeats are periodic signals sent from an activity to the Temporal server
    to indicate that the activity is still making progress. This decorator
    automatically sends these heartbeats at regular intervals.

    The heartbeat interval is calculated as 1/3 of the activity's configured
    heartbeat timeout. If no timeout is configured, it defaults to 120 seconds
    (resulting in a 40-second heartbeat interval).

    Args:
        fn: The activity function to be decorated. Must be an async function.

    Returns:
        The decorated activity function that includes automatic heartbeating.

    Note:
        This decorator is particularly useful for long-running activities where
        early failure detection is important. Without heartbeats, Temporal would
        have to wait for the entire activity timeout before detecting a failure.

        For more information, see:
        - https://temporal.io/blog/activity-timeouts
        - https://github.com/temporalio/samples-python/blob/main/custom_decorator/activity_utils.py

    Example:
        >>> @activity.defn
        >>> @auto_heartbeater
        >>> async def my_activity():
        ...     # This activity will automatically send heartbeats
        ...     await long_running_operation()
    """

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any):
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
            send_periodic_heartbeat(heartbeat_timeout.total_seconds() / 3)
        )
        try:
            # check if activity is async
            if asyncio.iscoroutinefunction(fn):
                return await fn(*args, **kwargs)
            else:
                logger.warning(
                    f"{fn.__name__} is not async, you should register heartbeats manually instead of using the @auto_heartbeater decorator"
                )
                return fn(*args, **kwargs)
        except Exception as e:
            print(f"Error in activity: {e}")
            raise e
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                # Wait for heartbeat cancellation to complete
                await asyncio.wait([heartbeat_task])

    return cast(F, wrapper)


async def send_periodic_heartbeat(delay: float, *details: Any) -> None:
    """Sends heartbeat signals at regular intervals until cancelled.

    This function runs in an infinite loop, sleeping for the specified delay between
    heartbeats. The heartbeat signals help Temporal track the activity's progress
    and detect failures.

    Args:
        delay: The delay between heartbeats in seconds.
        *details: Optional details to include in the heartbeat signal. These can be
            used to provide progress information or state that should be available
            if the activity needs to be retried.

    Note:
        This function is typically used internally by the @auto_heartbeater decorator
        and should not need to be called directly in most cases.

    Example:
        >>> # Send heartbeats every 30 seconds with a status message
        >>> heartbeat_task = asyncio.create_task(
        ...     send_periodic_heartbeat(30, "Processing items...")
        ... )
        >>> try:
        ...     await main_task
        ... finally:
        ...     heartbeat_task.cancel()
        ...     await asyncio.wait([heartbeat_task])
    """
    # Heartbeat every so often while not cancelled
    while True:
        await asyncio.sleep(delay)
        activity.heartbeat(*details)
