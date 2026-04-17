"""Utility functions for Temporal activities.

This module provides utility functions for working with Temporal activities,
including workflow ID retrieval, automatic heartbeating, and periodic heartbeat sending.
"""

import asyncio
import contextvars
import inspect
import os
import signal
import threading
from datetime import timedelta
from functools import wraps
from typing import Any, Callable, Optional, TypeVar, cast

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


def _build_direct_heartbeat_fn(
    ctx: contextvars.Context,
) -> Optional[Callable[[], None]]:
    """Try to build a heartbeat function that bypasses the asyncio event loop.

    Why this exists
    ---------------
    ``activity.heartbeat()`` for async activities routes through
    ``asyncio.Queue.put_nowait()`` + ``asyncio.create_task()`` — both of which
    require the event loop to be running and unblocked.  This is a problem when
    an async activity calls blocking sync code (e.g. a synchronous DB driver or
    ``time.sleep``): the event loop thread is occupied, so heartbeats queued
    via ``call_soon_threadsafe`` cannot fire until the block ends — potentially
    after Temporal's heartbeat timeout.

    The actual network call is ``bridge_worker.record_activity_heartbeat(proto)``
    which is a *synchronous Rust call* that hands proto bytes off to the Rust
    tokio runtime independently of Python asyncio.  For no-details heartbeats
    (the only kind ``auto_heartbeater`` sends), the ``await data_converter.encode``
    step is skipped entirely (``if details:`` evaluates to False), so the
    asyncio scaffolding is pure overhead.

    Extraction path
    ---------------
    ``ctx.heartbeat`` → ``_ActivityOutboundImpl.heartbeat`` (bound method)
    ``ctx.heartbeat.__self__`` → ``_ActivityOutboundImpl`` instance
    ``ctx.heartbeat.__self__._worker`` → ``_ActivityWorker`` instance
    ``ctx.heartbeat.__self__._worker._bridge_worker()`` → ``bridge.Worker``

    Returns ``None`` when not in an activity context (tests, local runs, or if
    SDK internals change).  ``_heartbeat_thread_target`` falls back to
    ``loop.call_soon_threadsafe`` in that case.
    """
    try:
        task_token: bytes = ctx.run(activity.info).task_token

        heartbeat_callable = ctx.run(lambda: activity._Context.current().heartbeat)
        if heartbeat_callable is None:
            return None

        outbound = getattr(heartbeat_callable, "__self__", None)
        if outbound is None:
            return None

        activity_worker = getattr(outbound, "_worker", None)
        if activity_worker is None:
            return None

        bridge_worker_fn = getattr(activity_worker, "_bridge_worker", None)
        if bridge_worker_fn is None:
            return None

        from temporalio.bridge.proto import ActivityHeartbeat as _ActivityHeartbeat

        proto = _ActivityHeartbeat(task_token=task_token)

        def direct_heartbeat() -> None:
            # Pure sync Rust call — no event loop required. Works even while
            # the event loop thread is blocked by synchronous code.
            bridge_worker_fn().record_activity_heartbeat(proto)

        logger.debug(
            "_build_direct_heartbeat_fn: direct bridge heartbeat available "
            "(task_token=%s…)",
            task_token[:8].hex(),
        )
        return direct_heartbeat

    except Exception:
        logger.debug(
            "_build_direct_heartbeat_fn: not in activity context, "
            "falling back to call_soon_threadsafe",
            exc_info=True,
        )
        return None


def _heartbeat_thread_target(
    delay: float,
    stop_event: threading.Event,
    loop: Optional[asyncio.AbstractEventLoop],
    ctx: contextvars.Context,
    direct_heartbeat_fn: Optional[Callable[[], None]] = None,
) -> None:
    """Background thread that sends Temporal heartbeats at regular intervals.

    Async activities (``loop`` is not None):
        **Primary path** — ``direct_heartbeat_fn`` is set: calls
        ``bridge_worker.record_activity_heartbeat(proto)`` directly from the
        background thread.  This is a synchronous Rust call that does not
        require the Python event loop to be running or unblocked, so heartbeats
        fire even while the event loop thread is executing blocking sync code.

        **Fallback path** — ``direct_heartbeat_fn`` is None (e.g. in tests or
        non-Temporal environments): uses ``loop.call_soon_threadsafe``.
        Callbacks are queued and fire as soon as the loop is free.

    Sync activities (``loop`` is None):
        Calls ``ctx.run(activity.heartbeat)`` directly.  The Temporal SDK wraps
        ``ctx.heartbeat`` with ``asyncio.run_coroutine_threadsafe`` for the
        thread-pool case, so this is thread-safe from any OS thread.

    Transient heartbeat exceptions are caught and logged — the thread
    continues so one failure does not permanently stop heartbeating.

    If the thread exits unexpectedly, SIGTERM is sent to trigger a container
    restart so Temporal reschedules the activity cleanly.
    """
    try:
        while not stop_event.wait(timeout=delay):
            try:
                if loop is not None:
                    if direct_heartbeat_fn is not None:
                        direct_heartbeat_fn()
                    else:
                        loop.call_soon_threadsafe(ctx.run, activity.heartbeat)
                else:
                    ctx.run(activity.heartbeat)
                logger.debug("_heartbeat_thread_target: heartbeat sent")
            except Exception:
                logger.warning(
                    "_heartbeat_thread_target: heartbeat failed, will retry",
                    exc_info=True,
                )
    except Exception:
        logger.error(
            "_heartbeat_thread_target: heartbeat thread crashed unexpectedly, "
            "sending SIGTERM to restart the worker",
            exc_info=True,
        )
        os.kill(os.getpid(), signal.SIGTERM)


def auto_heartbeater(fn: F) -> F:
    """Decorator that automatically sends heartbeats during activity execution.

    Uses a background thread at 1/3 of the configured heartbeat timeout
    (default 40 s for the 120 s default timeout).

    Async activities:
        At startup, extracts the Rust bridge worker and task token from the
        Temporal activity context via ``_build_direct_heartbeat_fn``.  The
        background thread calls ``bridge_worker.record_activity_heartbeat()``
        — a synchronous Rust call that does not require the Python asyncio
        event loop — so **heartbeats fire even when the event loop is blocked
        by synchronous code** (blocking DB drivers, ``time.sleep``, etc.).

        Falls back to ``loop.call_soon_threadsafe`` when bridge extraction
        fails (e.g. in tests or non-Temporal environments).

    Sync activities:
        Calls ``activity.heartbeat()`` via the copied context.  The Temporal
        SDK already wraps the heartbeat callable with
        ``asyncio.run_coroutine_threadsafe`` for the thread-pool case.

    If the heartbeat thread crashes unexpectedly, SIGTERM is sent so the
    container restarts and Temporal reschedules the activity.

    Example:
        >>> @activity.defn
        >>> @auto_heartbeater
        >>> async def my_activity():
        ...     # Blocking calls work fine — heartbeats fire from a background thread
        ...     result = blocking_db_call()
    """
    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            heartbeat_timeout = _get_heartbeat_timeout()
            delay = heartbeat_timeout.total_seconds() / 3

            stop_event = threading.Event()
            loop = asyncio.get_running_loop()
            ctx = contextvars.copy_context()
            direct_heartbeat_fn = _build_direct_heartbeat_fn(ctx)

            heartbeat_thread = threading.Thread(
                target=_heartbeat_thread_target,
                args=(delay, stop_event, loop, ctx, direct_heartbeat_fn),
                daemon=True,
            )
            heartbeat_thread.start()
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                logger.error("Error in activity: %s", e, exc_info=e)
                raise
            finally:
                stop_event.set()
                heartbeat_thread.join(timeout=5)

        return cast(F, async_wrapper)

    else:

        @wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            heartbeat_timeout = _get_heartbeat_timeout()
            delay = heartbeat_timeout.total_seconds() / 3

            stop_event = threading.Event()
            ctx = contextvars.copy_context()

            heartbeat_thread = threading.Thread(
                target=_heartbeat_thread_target,
                args=(delay, stop_event, None, ctx),
                daemon=True,
            )
            heartbeat_thread.start()
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                logger.error("Error in activity: %s", e, exc_info=e)
                raise
            finally:
                stop_event.set()
                heartbeat_thread.join(timeout=5)

        return cast(F, sync_wrapper)
