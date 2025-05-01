import asyncio
from datetime import timedelta
from functools import wraps
from typing import Any, Awaitable, Callable, Optional, TypeVar, cast

from temporalio import activity

from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def get_workflow_id() -> str:
    """Get the workflow ID from the current activity.

    Returns:
        str: The workflow ID of the current activity.
    Example:
        >>> workflow_id = get_workflow_id()
        >>> print(workflow_id)  # e.g. "my-workflow-123"

    Raises:
        RuntimeError: If called outside of an activity context.

    """
    try:
        return activity.info().workflow_id
    except Exception as e:
        logger.error("Failed to get workflow id", exc_info=e)
        raise Exception("Failed to get workflow id")


def auto_heartbeater(fn: F) -> F:
    """Auto-heartbeater for activities.

    Heartbeats are periodic signals sent from an activity to the Temporal server to indicate
    that the activity is still making progress. They help detect activity failures earlier
    by allowing the server to determine if an activity is stuck or has crashed, rather than
    waiting for the entire activity timeout. The auto_heartbeater decorator automatically
    sends these heartbeats at regular intervals.

    This timeout is set to 120 seconds by default.

    Args:
        fn (F): The activity function to be decorated.

    Returns:
        F: The decorated activity function.

    Note:
        We have activities that can run for a long time, in case of a failure (say: worker crash).

        Temporal will not retry the activity until the configured timeout is reached.

        We add auto_heartbeater to activities to ensure an failure is detected earlier
        and the activity is retried.

        - https://temporal.io/blog/activity-timeouts
        - https://github.com/temporalio/samples-python/blob/main/custom_decorator/activity_utils.py

    Example:
        >>> @activity.defn
        >>> @auto_heartbeater
        >>> async def my_activity():
        ...     pass
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


async def send_periodic_heartbeat(delay: float, *details: Any) -> None:
    """Sends heartbeat signals at regular intervals until the task is cancelled.

    This function runs in an infinite loop, sleeping for the specified delay between
    heartbeats. The heartbeat signals help Temporal track the activity's progress and
    detect failures.

    Example:
        >>> # Heartbeat every 30 seconds with a status message
        >>> heartbeat_task = asyncio.create_task(
        ...     send_periodic_heartbeat(30, "Processing items...")
        ... )
        >>> # Cancel when done
        >>> heartbeat_task.cancel()

    Args:
        delay (float): The delay between heartbeats in seconds.
        *details (Any): Variable length argument list of details to include in the heartbeat.
    """
    # Heartbeat every so often while not cancelled
    while True:
        await asyncio.sleep(delay)
        activity.heartbeat(*details)
