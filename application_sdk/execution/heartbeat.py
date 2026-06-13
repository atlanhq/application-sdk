"""Heartbeat support for long-running tasks.

Two modes of heartbeating are supported:

1. Automatic (framework-managed): background task sends heartbeats at configured
   intervals — zero developer effort.
2. Manual (developer-controlled): developer calls heartbeat() with progress info
   for resume-on-retry support.
"""

import asyncio
import concurrent.futures
import contextvars
import functools
import os
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Dedicated executor for blocking operations dispatched via run_in_thread().
#
# Why not None (asyncio's default executor)?
#   Temporal's Python SDK uses the event loop's default executor for its own
#   internal scheduling.  Sharing that pool with long-running blocking calls
#   (database queries, metadata extractions) can exhaust it and deadlock the
#   worker, especially when multiple activities are running concurrently.
#
# Why not a per-call ThreadPoolExecutor?
#   Creating one per call and calling shutdown(wait=False) leaks threads:
#   the executor object is detached but live threads are not joined and
#   accumulate over the lifetime of a worker process.
#
# This single instance is created once at module import and intentionally
# outlives individual calls.  Named threads ("sdk-blocking-N") make it
# distinguishable from Temporal's "activity-pool-N" threads in stack traces.
_BLOCKING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=min(32, (os.cpu_count() or 1) + 4),
    thread_name_prefix="sdk-blocking-",
)

T = TypeVar("T")


class HeartbeatController(Protocol):
    """Protocol for heartbeat operations."""

    def heartbeat(self, *details: Any) -> None:
        """Send a heartbeat with optional progress details."""
        ...

    def heartbeat_keepalive(self) -> None:
        """Send a keepalive heartbeat re-using the most recently set details."""
        ...

    def get_last_heartbeat_details(self) -> tuple[Any, ...]:
        """Get details from last heartbeat (for resume on retry)."""
        ...


class TemporalHeartbeatController:
    """HeartbeatController that uses Temporal's activity.heartbeat()."""

    def __init__(self) -> None:
        self._last_details: tuple[Any, ...] = ()

    def heartbeat(self, *details: Any) -> None:
        """Send a heartbeat to Temporal with optional progress details."""
        from temporalio import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            activity,
        )

        self._last_details = details
        activity.heartbeat(*details)

    def heartbeat_keepalive(self) -> None:
        """Send a keepalive heartbeat re-using the most recently set details."""
        from temporalio import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            activity,
        )

        activity.heartbeat(*self._last_details)

    def get_last_heartbeat_details(self) -> tuple[Any, ...]:
        """Get details from the last heartbeat before activity was retried."""
        from temporalio import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            activity,
        )

        return tuple(activity.info().heartbeat_details)


class NoopHeartbeatController:
    """No-op HeartbeatController for local execution and testing."""

    def __init__(self) -> None:
        self._details: tuple[Any, ...] = ()
        self._heartbeat_calls: list[tuple[Any, ...]] = []

    def heartbeat(self, *details: Any) -> None:
        """Record a heartbeat call."""
        self._details = details
        self._heartbeat_calls.append(details)

    def heartbeat_keepalive(self) -> None:
        """No-op keepalive for local/test execution."""
        self._heartbeat_calls.append(self._details)

    def get_last_heartbeat_details(self) -> tuple[Any, ...]:
        """Get the details from the last heartbeat call."""
        return self._details


async def auto_heartbeat_loop(
    interval_seconds: float,
    heartbeat_fn: Callable[[], None],
    stop_event: asyncio.Event,
    task_name: str,
) -> None:
    """Background task that sends heartbeats at regular intervals.

    Also monitors for event loop blocking and emits warnings if the loop
    is blocked for more than 50% of the heartbeat interval.

    CRITICAL: Auto-heartbeats only work when the event loop yields.
    They WILL FAIL for blocking I/O, CPU-bound computation, or long-running
    C extensions. Use run_in_thread() to wrap blocking operations.

    Args:
        interval_seconds: How often to send heartbeats.
        heartbeat_fn: Function to call for each heartbeat.
        stop_event: Event to signal loop termination.
        task_name: Name of the task (for warning messages).
    """
    warning_threshold = interval_seconds * 0.5

    while not stop_event.is_set():
        loop_start = time.monotonic()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except TimeoutError:  # conformance: ignore[E002] wait_for timeout = heartbeat interval elapsed; loop continues
            pass

        actual_elapsed = time.monotonic() - loop_start
        if actual_elapsed > interval_seconds + warning_threshold:
            blocked_time = actual_elapsed - interval_seconds
            logger.warning(
                "Event loop blocked for %.1fs during task %s, auto-heartbeating may be "
                "unreliable. Use self.task_context.run_in_thread() for blocking operations, "
                "or switch to manual heartbeating.",
                round(blocked_time, 1),
                task_name,
            )

        try:
            heartbeat_fn()
            logger.debug(
                "Auto-heartbeat sent for task '%s' (loop elapsed=%.2fs)",
                task_name,
                actual_elapsed,
            )
        except Exception as e:
            logger.warning(
                "Auto-heartbeat FAILED for task '%s': %s: %s",
                task_name,
                type(e).__name__,
                e,
                exc_info=True,
            )
        except BaseException:
            logger.debug(
                "Auto-heartbeat loop stopping: activity cancelled for task '%s'",
                task_name,
            )
            raise


async def run_in_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Last-resort escape hatch: run a blocking function in a thread pool.

    .. warning::
        **Use only when no async-native alternative exists.** This is the
        bottom of the preference list, not the default tool for "I have I/O
        to do". Per ADR-0010 (async-first design), the SDK runs on Temporal's
        asyncio event loop; blocking the loop breaks auto-heartbeats and
        causes activities to be retried even though they are making progress.

    **Decision order for blocking work (apps and SDK alike):**

    1. **Prefer an async-native library.** If one exists, use it. No
       ``run_in_thread`` needed:

       =========================  ======================  ====================
       Need                       Use (async)             Avoid (blocking)
       =========================  ======================  ====================
       HTTP requests              ``httpx``, ``aiohttp``  ``requests``
       AWS SDK                    ``aioboto3``,           ``boto3``
                                  ``aiobotocore``
       PostgreSQL                 ``asyncpg``             ``psycopg2``
       MySQL                      ``aiomysql``            ``pymysql``
       File I/O                   ``aiofiles``            ``open()``
       =========================  ======================  ====================

    2. **Then check the SDK.** Many helpers are already async — for example,
       ``self.context.storage`` (ObjectStore), ``self.context.state``
       (StateStore), and credential resolution all expose ``await``-able
       methods. Don't wrap them in ``run_in_thread``.
    3. **Only then** fall back to ``run_in_thread`` — and only after
       confirming there is no async-native alternative for the library
       you're calling.

    **Examples of incorrect use (do not do this):**

    .. code-block:: python

        # WRONG — boto3 has aioboto3; use that instead.
        await self.task_context.run_in_thread(s3_client.put_object, ...)

        # WRONG — requests has httpx; use that instead.
        await self.task_context.run_in_thread(requests.get, url, timeout=30)

    **Behavior:**

    - ContextVars (ObjectStore, logger context, correlation ID, infrastructure
      handles) are propagated to the worker thread via
      ``contextvars.copy_context()``. Mutations inside the thread stay
      isolated from the caller (copy semantics).
    - Threads run on a dedicated ``sdk-blocking-*`` pool, separate from
      Temporal's activity pool, to avoid deadlocking the worker.

    **CRITICAL: your blocking code MUST have its own timeout.**
    Python threads cannot be forcibly killed. If the wrapped call hangs
    forever, the thread runs forever — this orphans state and consumes
    pool slots even after the activity is retried.

    Args:
        func: Blocking function to run. MUST have internal timeout handling.
        *args: Positional arguments for ``func``.
        **kwargs: Keyword arguments for ``func``.

    Returns:
        Result of ``func(*args, **kwargs)``.

    See Also:
        - ``docs/adr/0010-async-first-blocking-code.md`` — full rationale.
        - ``self.context.storage`` / ``self.context.state`` — already async.
    """
    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _BLOCKING_EXECUTOR,
        functools.partial(ctx.run, functools.partial(func, *args, **kwargs)),
    )
