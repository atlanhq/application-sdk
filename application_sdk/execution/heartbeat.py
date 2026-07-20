"""Heartbeat support for long-running tasks.

Two modes of heartbeating are supported:

1. Automatic (framework-managed): background task sends heartbeats at configured
   intervals — zero developer effort.
2. Manual (developer-controlled): developer calls heartbeat() with progress info
   for resume-on-retry support.

This module is also the SDK's offload seam, with three sanctioned primitives:
``run_in_thread`` offloads blocking calls so they don't starve the heartbeat
loop (ADR-0010); ``run_fault_isolated`` runs work in a child process so a native
fault can't kill the worker; and ``run_best_effort`` is the policy layer over it
for non-essential work — it isolates *and* swallows failures so best-effort work
can never break the caller.
"""

import asyncio
import concurrent.futures
import contextvars
import functools
import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Protocol, TypeVar

from application_sdk.observability import (
    resource_sampler as _resource_sampler,  # module alias kept so tests can patch _resource_sampler.sample()
)
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter, get_logger
from application_sdk.observability.resource_sampler import parse_pod_memory_limit

logger = get_logger(__name__)

_MEMORY_WARN_THRESHOLD = 0.80
_MEMORY_WARN_HYSTERESIS = (
    0.05  # re-arm only once ratio drops below threshold - hysteresis
)

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
    _limit_bytes = parse_pod_memory_limit(os.environ.get("K8S_POD_MEMORY_LIMIT", ""))
    _memory_warn_active = False

    while not stop_event.is_set():
        loop_start = time.monotonic()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except TimeoutError:  # conformance: ignore[E002,E014] wait_for timeout = heartbeat interval elapsed; loop continues
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
        # conformance: ignore[E004] catch-and-reraise for Temporal CancelledError; logged at debug then immediately re-raised
        except BaseException:
            logger.debug(
                "Auto-heartbeat loop stopping: activity cancelled for task '%s'",
                task_name,
            )
            raise

        if _limit_bytes > 0:
            try:
                _mem = _resource_sampler.sample()
                if _mem is not None:
                    _ratio = _mem.rss_bytes / _limit_bytes
                    if not _memory_warn_active and _ratio >= _MEMORY_WARN_THRESHOLD:
                        _memory_warn_active = True
                        logger.warning(
                            "Memory pressure on task '%s': %.0f%% of limit (%.2f GiB / %.2f GiB)"
                            " — OOM kill imminent if this continues rising",
                            task_name,
                            _ratio * 100,
                            _mem.rss_bytes / (1024**3),
                            _limit_bytes / (1024**3),
                        )
                    elif (
                        _memory_warn_active
                        and _ratio < _MEMORY_WARN_THRESHOLD - _MEMORY_WARN_HYSTERESIS
                    ):
                        _memory_warn_active = False
            # conformance: ignore[E004] best-effort memory sampling must never interrupt the heartbeat loop; logged at DEBUG (not warning/error) since transient sampling failures are expected and non-actionable
            except Exception as e:
                # Best-effort; must never interrupt the heartbeat loop.
                logger.debug(
                    "Memory sampling failed for task '%s': %s",
                    task_name,
                    e,
                    exc_info=True,
                )


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


# Executor for work that must not be able to take the worker down with it.
#
# Why a process, not a thread?
#   A native fault (SIGSEGV in a C extension) is not a Python exception: it
#   bypasses every try/except and kills the whole process. In a thread that
#   means the Temporal worker dies mid-poll. In a child process the kernel
#   kills only the child, and the parent observes an ordinary, catchable
#   BrokenProcessPool.
#
# Why spawn, not fork?
#   fork() in a multi-threaded process (a Temporal worker always is) copies a
#   single thread but every lock, in whatever state the other threads left
#   them — a deadlock/corruption factory. spawn starts a clean interpreter.
#
# Lazy, single-worker, discarded on crash or timeout and re-created on the
# next call. Created only when run_fault_isolated is first used, so processes
# that never need isolation never pay for the child.
_PROCESS_EXECUTOR: concurrent.futures.ProcessPoolExecutor | None = None
_PROCESS_EXECUTOR_LOCK = threading.Lock()


def _get_process_executor() -> concurrent.futures.ProcessPoolExecutor:
    global _PROCESS_EXECUTOR
    with _PROCESS_EXECUTOR_LOCK:
        if _PROCESS_EXECUTOR is None:
            _PROCESS_EXECUTOR = concurrent.futures.ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn"),
            )
        return _PROCESS_EXECUTOR


def _discard_process_executor() -> None:
    """Drop the pool (and kill its child) so the next call starts fresh."""
    global _PROCESS_EXECUTOR
    with _PROCESS_EXECUTOR_LOCK:
        executor, _PROCESS_EXECUTOR = _PROCESS_EXECUTOR, None
    if executor is None:
        return
    # Kill the children BEFORE shutdown(): a dead child is the pool's
    # well-trodden unwind path — the manager thread sees it, marks the pool
    # broken, resolves every still-queued work item with BrokenProcessPool,
    # and every internal thread exits. The reverse order (shutdown, then kill)
    # strands a manager/feeder thread on a lock and hangs interpreter exit.
    # No cancel_futures=True: cancelling a *foreign* caller's queued future
    # would surface as CancelledError (a BaseException) in that innocent
    # caller; the broken-pool resolution reaches it as a catchable
    # BrokenProcessPool instead. shutdown() never kills a *running* child
    # (e.g. one hung past a timeout) and ProcessPoolExecutor exposes no
    # supported kill, so reach for the internal process table (None once the
    # pool is broken); on a future CPython that renames it, the child leaks
    # until it finishes — degraded, not fatal.
    for process in list((getattr(executor, "_processes", None) or {}).values()):
        process.kill()
    executor.shutdown(wait=False)


async def run_fault_isolated(
    func: Callable[..., T], *args: Any, timeout: float | None = None, **kwargs: Any
) -> T:
    """Run ``func`` in an isolated child process (native-crash containment).

    The mechanism layer. Unlike :func:`run_in_thread`, this survives faults that
    are not Python exceptions: if ``func`` segfaults a C extension, only the
    child dies and the caller gets a catchable :class:`BrokenProcessPool`. Use it
    for work whose native fault must never take the worker process down.

    This *raises* on failure (``BrokenProcessPool`` / ``TimeoutError``) — the
    caller decides what to do. For non-essential work that should be silently
    skipped on failure, prefer :func:`run_best_effort`, which wraps this and
    swallows failures. Essential work — where a failure should fail the activity
    — should not be isolated per-call at all; run it in-process or via
    :func:`run_in_thread` and let Temporal/k8s recover a crash.

    Constraints that :func:`run_in_thread` does not have:

    - ``func``, ``args``, ``kwargs`` and the return value must be picklable;
      ``func`` must be a module-level function (pickled by reference).
    - ContextVars do **not** propagate — the child is a fresh interpreter.
      Have the child return data and log from the parent.
    - The child imports ``func``'s module on first use (one-time cost,
      amortized by the pooled worker).

    Args:
        func: Module-level function to run in the child.
        timeout: Seconds to wait before killing the child and raising
            ``TimeoutError``. ``None`` waits forever.

    Raises:
        BrokenProcessPool: The child died abnormally (native crash), or a
            concurrent caller's timeout discarded the shared pool while this
            call was queued. The pool is discarded; the next call gets a
            fresh child.
        TimeoutError: ``timeout`` elapsed. The child is killed and the pool
            discarded.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        _get_process_executor(), functools.partial(func, *args, **kwargs)
    )
    if timeout is not None:
        # Not asyncio.wait_for: on timeout it cancels the future and then waits
        # for the cancellation to land — but a running executor call cannot be
        # cancelled, so wait_for would hang exactly when the child hangs.
        # asyncio.wait just stops waiting; we then kill the child ourselves.
        done, _ = await asyncio.wait({future}, timeout=timeout)
        if not done:
            _discard_process_executor()
            # The kill resolves the abandoned future with BrokenProcessPool;
            # consume it so asyncio never logs "exception was never retrieved".
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            raise TimeoutError(f"run_fault_isolated timed out after {timeout}s")
    try:
        return await future
    except BrokenProcessPool:
        _discard_process_executor()
        raise
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise  # real cancellation of the caller — must propagate
        # Foreign cancellation: a concurrent caller's timeout discarded the
        # shared pool while this call was still queued. From this caller's
        # perspective that is exactly a broken pool — surface it as the
        # catchable exception the contract promises, never CancelledError.
        raise BrokenProcessPool(
            "process pool was discarded while this call was queued"
        ) from None


async def run_best_effort(
    func: Callable[..., T],
    *args: Any,
    label: str,
    logger: AtlanLoggerAdapter,
    timeout: float | None = None,
    **kwargs: Any,
) -> T | None:
    """Run non-essential native work fault-isolated; never let it break the caller.

    The policy layer over :func:`run_fault_isolated`. Runs ``func`` in an
    isolated child process and, on *any* failure — a native crash
    (``BrokenProcessPool``), a ``timeout``, or an ordinary exception — logs a
    warning via ``logger`` and returns ``None`` rather than propagating. This is
    the SDK's sanctioned home for *best-effort* native work: work whose result is
    used when present and safely skipped when absent, and which must never crash
    or fail the worker (e.g. the warn-only upload validation scan). Essential
    work — where a failure *should* fail the activity — must not use this.

    A genuine caller cancellation (``asyncio.CancelledError`` from cooperative
    task cancellation) is deliberately **not** swallowed — it propagates.

    Args:
        func: Module-level function to run in the child. Same picklability /
            ContextVar constraints as :func:`run_fault_isolated`.
        label: Human label for the work, interpolated into the warning
            (e.g. ``"Transformed-asset validation"``).
        logger: The caller's logger, so the warning is attributed to the
            caller's module (OTel source) rather than this one.
        timeout: Seconds before the child is killed and the run is skipped.

    Returns:
        ``func``'s result, or ``None`` if the run crashed, timed out, or errored.
    """
    try:
        return await run_fault_isolated(func, *args, timeout=timeout, **kwargs)
    except BrokenProcessPool:
        logger.warning(
            "%s subprocess died or was discarded (a native fault in a "
            "dependency, or a concurrent call's timeout); continuing without it",
            label,
        )
    except TimeoutError:
        logger.warning("%s timed out after %ss; continuing without it", label, timeout)
    except Exception:  # noqa: BLE001 — best-effort work must never break the caller
        logger.warning("%s skipped due to an unexpected error", label, exc_info=True)
    return None
