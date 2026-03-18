"""Heartbeat support for long-running tasks.

Two modes of heartbeating are supported:
1. Automatic (framework-managed): background task sends heartbeats at configured
   intervals — zero developer effort.
2. Manual (developer-controlled): developer calls heartbeat() with progress info
   for resume-on-retry support.
"""

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

logger = logging.getLogger(__name__)

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
        from temporalio import activity

        self._last_details = details
        activity.heartbeat(*details)

    def heartbeat_keepalive(self) -> None:
        """Send a keepalive heartbeat re-using the most recently set details."""
        from temporalio import activity

        activity.heartbeat(*self._last_details)

    def get_last_heartbeat_details(self) -> tuple[Any, ...]:
        """Get details from the last heartbeat before activity was retried."""
        from temporalio import activity

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
        except TimeoutError:
            pass

        actual_elapsed = time.monotonic() - loop_start
        if actual_elapsed > interval_seconds + warning_threshold:
            blocked_time = actual_elapsed - interval_seconds
            logger.warning(
                f"Event loop blocked for {blocked_time:.1f}s during task '{task_name}'. "
                f"Auto-heartbeating may be unreliable. Use self.task_context.run_in_thread() "
                f"for blocking operations, or switch to manual heartbeating."
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
    """Run a blocking function in a thread pool.

    Use this for blocking I/O or CPU-bound operations to keep the event loop
    responsive for heartbeating.

    CRITICAL: YOUR BLOCKING CODE MUST HAVE ITS OWN TIMEOUTS.
    Python threads cannot be forcibly killed. If your blocking code hangs
    indefinitely, the thread will run forever.

    Args:
        func: Blocking function to run. MUST have internal timeout handling.
        *args: Positional arguments for func.
        **kwargs: Keyword arguments for func.

    Returns:
        Result of func(*args, **kwargs).
    """
    import concurrent.futures

    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=4, thread_name_prefix="run_in_thread"
    )
    try:
        result = await loop.run_in_executor(
            executor,
            functools.partial(func, *args, **kwargs),
        )
        return result
    finally:
        executor.shutdown(wait=False)
