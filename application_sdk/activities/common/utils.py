"""Utility functions for Temporal activities.

This module provides utility functions for working with Temporal activities,
including workflow ID retrieval, automatic heartbeating, and periodic heartbeat sending.
"""

import asyncio
import glob
import os
from datetime import timedelta
from functools import wraps
from typing import Any, Awaitable, Callable, List, Optional, TypeVar, cast

from temporalio import activity

from application_sdk.constants import (
    APPLICATION_NAME,
    TEMPORARY_PATH,
    WORKFLOW_OUTPUT_PATH_TEMPLATE,
)
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


def find_local_files_by_extension(
    path: str,
    extension: str,
    file_names: Optional[List[str]] = None,
) -> List[str]:
    """Find local files at the specified local path, optionally filtering by file names.

    Args:
        path (str): Local path to search in (file or directory)
        extension (str): File extension to filter by (e.g., '.parquet', '.json')
        file_names (Optional[List[str]]): List of file names (basenames) to filter by, paths are not supported

    Returns:
        List[str]: List of matching file paths

    Example:
        >>> find_local_files_by_extension("/data", ".parquet", ["file1.parquet", "file2.parquet"])
        ['file1.parquet', 'file2.parquet']

        >>> find_local_files_by_extension("/data/single.json", ".json")
        ['single.json']
    """
    if os.path.isfile(path) and path.endswith(extension):
        # Single file - return it directly
        return [path]

    elif os.path.isdir(path):
        # Directory - find all files in directory
        all_files = glob.glob(
            os.path.join(path, "**", f"*{extension}"),
            recursive=True,
        )

        # Filter by file names if specified
        if file_names:
            file_names_set = set(file_names)  # Convert to set for O(1) lookup
            return [f for f in all_files if os.path.basename(f) in file_names_set]
        else:
            return all_files

    return []
