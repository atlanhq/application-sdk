"""Utility functions for Temporal activities.

This module provides utility functions for working with Temporal activities,
including workflow ID retrieval, automatic heartbeating, and periodic heartbeat sending.
"""

import asyncio
import contextvars
import inspect
import os
import threading
from datetime import timedelta
from functools import wraps
from typing import Any, Callable, TypeVar, cast

from temporalio import activity

from application_sdk.constants import (
    APPLICATION_NAME,
    TEMPORARY_PATH,
    WORKFLOW_OUTPUT_PATH_TEMPLATE,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


F = TypeVar("F", bound=Callable[..., Any])


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


def get_workflow_run_id() -> str:
    """Get the workflow run ID from the current activity."""
    try:
        return activity.info().workflow_run_id
    except Exception as e:
        logger.error("Failed to get workflow run id", exc_info=e)
        raise Exception("Failed to get workflow run id")


def build_output_path() -> str:
    """Build a standardized output path for workflow artifacts.

    This method creates a consistent output path format across all workflows using the WORKFLOW_OUTPUT_PATH_TEMPLATE constant.

    Returns:
        str: The standardized output path.

    Example:
        >>> build_output_path()
        "artifacts/apps/appName/workflows/wf-123/run-456"
    """
    return WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
        application_name=APPLICATION_NAME,
        workflow_id=get_workflow_id(),
        run_id=get_workflow_run_id(),
    )


def get_object_store_prefix(path: str) -> str:
    """Get the object store prefix for the path.

    This function handles two types of paths:
    1. Paths under TEMPORARY_PATH - converts them to relative object store paths
    2. User-provided paths - returns them as-is (already relative object store paths)

    Args:
        path: The path to convert to object store prefix.

    Returns:
        The object store prefix for the path.

    Examples:
        >>> # Temporary path case
        >>> get_object_store_prefix("./local/tmp/artifacts/apps/appName/workflows/wf-123/run-456")
        "artifacts/apps/appName/workflows/wf-123/run-456"

        >>> # User-provided path case
        >>> get_object_store_prefix("datasets/sales/2024/")
        "datasets/sales/2024"
    """
    # Normalize paths for comparison
    abs_path = os.path.abspath(path)
    abs_temp_path = os.path.abspath(TEMPORARY_PATH)

    # Check if path is under TEMPORARY_PATH
    try:
        # Use os.path.commonpath to properly check if path is under temp directory
        # This prevents false positives like '/tmp/local123' matching '/tmp/local'
        common_path = os.path.commonpath([abs_path, abs_temp_path])
        if common_path == abs_temp_path:
            # Path is under temp directory, convert to relative object store path
            relative_path = os.path.relpath(abs_path, abs_temp_path)
            # Normalize path separators to forward slashes for object store
            return relative_path.replace(os.path.sep, "/")
        else:
            # Path is already a relative object store path, return as-is
            return path.strip("/")
    except ValueError:
        # os.path.commonpath or os.path.relpath can raise ValueError on Windows with different drives
        # In this case, treat as user-provided path, return as-is
        return path.strip("/")


def _get_heartbeat_timeout() -> timedelta:
    """Get the heartbeat timeout from the current activity context.

    Returns:
        The configured heartbeat timeout, or a default of 120 seconds if not configured
        or if called outside of an activity context.
    """
    default_heartbeat_timeout = timedelta(seconds=120)
    try:
        timeout = activity.info().heartbeat_timeout
        return timeout if timeout else default_heartbeat_timeout
    except RuntimeError:
        return default_heartbeat_timeout


def _start_heartbeat_thread(
    stop_event: threading.Event,
    thread_failed: threading.Event,
) -> threading.Thread:
    """Start a daemon thread that sends heartbeats at regular intervals.

    Uses a background thread instead of an asyncio task so that heartbeats
    are never blocked by event loop starvation (e.g. sync code running
    inside an async activity).

    If the heartbeat thread exits unexpectedly (e.g. due to a BaseException
    that escapes the retry logic), it sets ``thread_failed`` so that the
    calling activity can detect the failure and stop early.

    Args:
        stop_event: Threading event used to signal the thread to stop.
        thread_failed: Threading event set by the thread on unexpected exit.

    Returns:
        The started heartbeat thread.
    """
    heartbeat_timeout = _get_heartbeat_timeout()
    delay = heartbeat_timeout.total_seconds() / 3
    ctx = contextvars.copy_context()

    def _run_heartbeat():
        try:
            ctx.run(send_periodic_heartbeat_sync, delay, stop_event)
        except BaseException as e:
            logger.error("Heartbeat thread crashed unexpectedly: %s", e, exc_info=e)
            thread_failed.set()

    heartbeat_thread = threading.Thread(target=_run_heartbeat, daemon=True)
    heartbeat_thread.start()
    return heartbeat_thread


async def _wait_for_thread_failure(
    thread_failed: threading.Event, check_interval: float = 1.0
) -> None:
    """Poll until the heartbeat thread signals an unexpected exit.

    Used as a concurrent task inside the async activity wrapper so that
    ``asyncio.wait`` can race the activity against heartbeat-thread health.

    Args:
        thread_failed: Event that the heartbeat thread sets on crash.
        check_interval: Seconds between polls.
    """
    while not thread_failed.is_set():
        await asyncio.sleep(check_interval)


def auto_heartbeater(fn: F) -> F:
    """Decorator that automatically sends heartbeats during activity execution.

    Heartbeats are periodic signals sent from an activity to the Temporal server
    to indicate that the activity is still making progress. This decorator
    automatically sends these heartbeats at regular intervals.

    The heartbeat interval is calculated as 1/3 of the activity's configured
    heartbeat timeout. If no timeout is configured, it defaults to 120 seconds
    (resulting in a 40-second heartbeat interval).

    Heartbeats are always sent from a background thread, regardless of whether
    the activity is async or sync. This ensures heartbeats are never starved
    by event loop blocking (e.g. sync calls inside async activities).

    Args:
        fn: The activity function to be decorated. Can be sync or async.

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
        >>> async def my_async_activity():
        ...     await long_running_operation()

        >>> @activity.defn
        >>> @auto_heartbeater
        >>> def my_sync_activity():
        ...     cpu_intensive_work()
    """

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any):
            stop_event = threading.Event()
            thread_failed = threading.Event()
            heartbeat_thread = _start_heartbeat_thread(stop_event, thread_failed)
            try:
                activity_task = asyncio.ensure_future(fn(*args, **kwargs))
                monitor_task = asyncio.ensure_future(
                    _wait_for_thread_failure(thread_failed)
                )
                done, pending = await asyncio.wait(
                    {activity_task, monitor_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                    try:
                        await p
                    except (asyncio.CancelledError, Exception):
                        pass

                if activity_task in done:
                    return activity_task.result()

                raise RuntimeError("Heartbeat thread stopped unexpectedly")
            except Exception as e:
                logger.error("Error in activity: %s", e, exc_info=e)
                raise
            finally:
                stop_event.set()
                heartbeat_thread.join(timeout=5)

        return cast(F, async_wrapper)
    else:

        @wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any):
            stop_event = threading.Event()
            thread_failed = threading.Event()
            heartbeat_thread = _start_heartbeat_thread(stop_event, thread_failed)
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                logger.error("Error in activity: %s", e, exc_info=e)
                raise
            finally:
                stop_event.set()
                heartbeat_thread.join(timeout=5)
            if thread_failed.is_set():
                raise RuntimeError("Heartbeat thread stopped unexpectedly")
            return result

        return cast(F, sync_wrapper)


def send_periodic_heartbeat_sync(
    delay: float, stop_event: threading.Event, *details: Any
) -> None:
    """Sends heartbeat signals at regular intervals from a background thread.

    This function runs in a loop, waiting on the stop_event for the specified delay.
    When the event is set, the loop exits cleanly.

    On transient failures, applies exponential backoff (capped at 5x the base delay)
    to avoid hammering an unhealthy Temporal server. Resets to normal interval after
    a successful heartbeat.

    Args:
        delay: The delay between heartbeats in seconds.
        stop_event: Threading event used to signal the thread to stop.
        *details: Optional details to include in the heartbeat signal.

    Note:
        This function is used internally by the @auto_heartbeater decorator for
        sync activity functions and should not need to be called directly.
    """
    consecutive_failures = 0
    # Cap backoff at the heartbeat timeout (delay * 3) so we never wait
    # longer than Temporal's patience window before retrying.
    max_delay = delay * 3
    current_delay = delay

    while not stop_event.wait(timeout=current_delay):
        try:
            activity.heartbeat(*details)
            if consecutive_failures > 0:
                logger.info(
                    "Heartbeat recovered after %d consecutive failures",
                    consecutive_failures,
                )
            consecutive_failures = 0
            current_delay = delay
        except Exception as e:
            consecutive_failures += 1
            current_delay = min(delay * 2**consecutive_failures, max_delay)
            logger.warning(
                "Heartbeat failed (attempt %d), retrying in %.1fs: %s",
                consecutive_failures,
                current_delay,
                e,
            )
