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
import math
import multiprocessing
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Protocol, TypeVar

from application_sdk.execution.progress import (
    ProgressTracker,
    ProgressWatchdogMode,
    current_progress_tracker,
)
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

#: Meter for signals this module owns. Deliberately not the Temporal meter: the
#: stall watchdog has no Temporal dependency, so it also reports from local runs.
_METER_NAME = "application_sdk.execution"

#: Lazily created singletons — one instrument per process, built on first use so
#: nothing is bound before ``run_main()`` configures the ``MeterProvider``.
_INSTRUMENTS: dict[str, Any] = {}

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


def _no_progress_gap_histogram() -> Any:
    """Gap-duration histogram, created on first stall observation.

    A histogram rather than a counter because the fleet-wide *distribution* of
    no-progress gaps is what sizes ``max_no_progress_seconds`` before anything
    enforces (ADR-0018 → *Decisions taken*).
    """
    if "no_progress_gap" not in _INSTRUMENTS:
        from opentelemetry import (  # noqa: PLC0415 — cold path: only reached on a stall observation
            metrics as _otel_metrics,
        )

        _INSTRUMENTS["no_progress_gap"] = _otel_metrics.get_meter(
            _METER_NAME
        ).create_histogram(
            "task.no_progress_gap",
            unit="s",
            description=(
                "Seconds a task attempt went without an observable progress "
                "signal, recorded once per gap that exceeded "
                "max_no_progress_seconds. Partitioned by task, the last "
                "progress label seen, and the watchdog mode — so warn-mode "
                "observations and enforced kills aggregate separately."
            ),
        )
    return _INSTRUMENTS["no_progress_gap"]


def _record_no_progress_gap(
    task_name: str, stalled_for: float, last_label: str, mode: ProgressWatchdogMode
) -> None:
    """Record one no-progress gap. Never raises.

    The metric is emitted in *both* warn and enforce mode: in warn mode it is
    the whole point (nothing else reports the gap), and in enforce mode it is
    what makes the kill rate measurable.
    """
    try:
        _no_progress_gap_histogram().record(
            stalled_for,
            {
                "task.name": task_name,
                # A progress label identifies a call site, never a value — see
                # ProgressTracker.enter_hold — so this stays bounded.
                "progress.last_label": last_label,
                "watchdog.mode": mode.value,
            },
        )
    # Best-effort observability: a metric-backend problem must never fail the
    # activity the watchdog is only observing.
    except Exception:
        logger.warning(
            "Failed to record the no-progress gap metric for task '%s' (%.0fs gap); "
            "the stall is still logged but will be missing from the fleet-wide "
            "gap distribution",
            task_name,
            stalled_for,
            exc_info=True,
        )


def _check_for_stall(
    progress: ProgressTracker,
    budget_seconds: float,
    mode: ProgressWatchdogMode,
    task_name: str,
    on_stall: Callable[[float, str], None] | None,
) -> bool:
    """Report a no-progress gap if one is open. Returns True to stop the loop.

    Called once per heartbeat tick, so detection latency is ``budget_seconds``
    plus at most one tick.

    Args:
        progress: The attempt's tracker. ``stalled_for()`` already accounts for
            holds, so a vouched-for operation reads as no gap at all.
        budget_seconds: ``max_no_progress_seconds`` for this task.
        mode: ``WARN`` reports; ``ENFORCE`` reports and fails the activity.
        task_name: Task name, for the log and the metric.
        on_stall: Injected by the activity layer to fail the attempt — the
            watchdog itself stays free of activity/Temporal semantics. Only
            called in ``ENFORCE``.

    Returns:
        ``True`` when the caller must return immediately: the watchdog *is*
        ``heartbeat_task``, and the activity's ``finally`` sets ``stop_event``
        and awaits it with a 1s bound, so it must not keep looping after
        deciding to fail the attempt.
    """
    stalled = progress.stalled_for()
    if stalled < budget_seconds:
        return False

    last_label = progress.last_label
    _record_no_progress_gap(task_name, stalled, last_label, mode)

    if mode is not ProgressWatchdogMode.ENFORCE or on_stall is None:
        # INFO, never WARNING: under a fleet-wide default this is an expected
        # observation, and emitting it at WARNING would manufacture exactly the
        # alert noise ADR-0018 exists to reduce. Dashboards read the metric.
        logger.info(
            "Task '%s' made no observable progress for %.0fs (budget %.0fs); last "
            "signal was '%s' — not failing (warn mode)",
            task_name,
            stalled,
            budget_seconds,
            last_label or "<none>",
        )
        # Re-arm so each gap is reported once rather than every tick. The empty
        # label deliberately leaves last_label alone: the signal just reported
        # is still the most useful thing to name in the next report.
        progress.mark_progress()
        return False

    logger.warning(
        "Task '%s' made no observable progress for %.0fs (budget %.0fs); last "
        "signal was '%s' — failing the activity",
        task_name,
        stalled,
        budget_seconds,
        last_label or "<none>",
    )
    try:
        on_stall(stalled, last_label)
    # The watchdog must not keep beating for an attempt it has already decided
    # to fail; the failure is handled by stopping the loop below.
    except Exception:
        logger.warning(
            "Stall handler for task '%s' raised, so the attempt was not failed "
            "in-process; stopping the heartbeat loop so Temporal's "
            "heartbeat_timeout reclaims it instead",
            task_name,
            exc_info=True,
        )
    return True


async def auto_heartbeat_loop(
    interval_seconds: float,
    heartbeat_fn: Callable[[], None],
    stop_event: asyncio.Event,
    task_name: str,
    *,
    progress: ProgressTracker | None = None,
    max_no_progress_seconds: float | None = None,
    watchdog_mode: ProgressWatchdogMode = ProgressWatchdogMode.OFF,
    on_stall: Callable[[float, str], None] | None = None,
) -> None:
    """Background task that sends heartbeats at regular intervals.

    Also monitors for event loop blocking and emits warnings if the loop
    is blocked for more than 50% of the heartbeat interval.

    CRITICAL: Auto-heartbeats only work when the event loop yields.
    They WILL FAIL for blocking I/O, CPU-bound computation, or long-running
    C extensions. Use run_in_thread() to wrap blocking operations.

    When a ``progress`` tracker and a budget are supplied, the same loop also
    runs the ADR-0018 **stall watchdog**. The beat itself stays
    **unconditional** — it is the crash detector (OOM, SIGKILL, node loss,
    partition, starved loop) and ``heartbeat_timeout`` keeps its meaning and its
    60s default. The watchdog is a second, independent question asked on the
    same tick: *has this attempt done anything observable lately?*

    Args:
        interval_seconds: How often to send heartbeats.
        heartbeat_fn: Function to call for each heartbeat.
        stop_event: Event to signal loop termination.
        task_name: Name of the task (for warning messages).
        progress: The attempt's :class:`ProgressTracker`. ``None`` leaves the
            watchdog inert.
        max_no_progress_seconds: How long this task may go without an
            observable progress signal. ``None`` leaves the watchdog inert.
        watchdog_mode: ``OFF`` (inert), ``WARN`` (report only) or ``ENFORCE``
            (report, then fail the attempt via ``on_stall``).
        on_stall: Called as ``on_stall(stalled_for, last_label)`` when a stall
            is enforced. Injected by the activity layer — the same way
            ``heartbeat_fn`` already is — so this module stays free of
            activity/Temporal semantics and the watchdog is unit-testable
            without a worker.
    """
    warning_threshold = interval_seconds * 0.5
    _limit_bytes = parse_pod_memory_limit(os.environ.get("K8S_POD_MEMORY_LIMIT", ""))
    _memory_warn_active = False

    watchdog_budget: float | None = (
        max_no_progress_seconds
        if progress is not None and watchdog_mode is not ProgressWatchdogMode.OFF
        else None
    )
    if watchdog_budget is not None and (
        not math.isfinite(watchdog_budget) or watchdog_budget <= 0
    ):
        # Refuse rather than obey. A budget of zero makes every attempt stall on
        # its first tick, so honouring it in enforce mode would turn one bad
        # config value into a fleet-wide kill switch. NaN slips past the
        # `<= 0` check (comparisons against NaN are always False) and would
        # enforce on the first tick, while +inf would silently never enforce —
        # so only a finite positive allowance counts as a real budget.
        logger.warning(
            "Stall watchdog for task '%s' was given a non-positive or "
            "non-finite no-progress budget (%ss); disabling the watchdog — set "
            "max_no_progress_seconds to a finite positive allowance, or the "
            "mode to 'off' to disable it deliberately",
            task_name,
            watchdog_budget,
        )
        watchdog_budget = None
    if (
        watchdog_budget is not None
        and watchdog_mode is ProgressWatchdogMode.ENFORCE
        and on_stall is None
    ):
        # A wiring bug, not a config choice. Downgrade rather than raise: the
        # watchdog must never itself be the thing that fails an activity, and
        # reporting is strictly better than going silent. Downgraded here rather
        # than at the call site so the metric's mode attribute describes what
        # the watchdog actually did.
        logger.warning(
            "Stall watchdog for task '%s' was asked to enforce but no on_stall "
            "handler was injected; reporting gaps without failing the attempt",
            task_name,
        )
        watchdog_mode = ProgressWatchdogMode.WARN

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

        # The stall watchdog runs last in the tick: the beat is the crash
        # detector and goes out first, and an enforced stall returns from here,
        # so putting it after the memory sample means the tick that kills a
        # wedged attempt still carries that attempt's last memory observation.
        if (
            progress is not None
            and watchdog_budget is not None
            and _check_for_stall(
                progress=progress,
                budget_seconds=watchdog_budget,
                mode=watchdog_mode,
                task_name=task_name,
                on_stall=on_stall,
            )
        ):
            return


#: Label prefixes for the auto-holds on the two offload seams (ADR-0018 →
#: *Feeding the tracker*, mechanism 2). The offloaded callable's own name is
#: appended, so warn mode ranks *sites* — "run_in_thread.Cursor.execute, p99
#: 40min, unbounded" is a work-list entry — instead of collapsing every blocking
#: call in an app into one bucket. Naming the seam is part of the signal: it
#: tells an operator which primitive to wrap in ``holding_progress()``.
_THREAD_HOLD_PREFIX = "run_in_thread."
_ISOLATED_HOLD_PREFIX = "run_fault_isolated."


def _offloaded_callable_name(func: Callable[..., Any]) -> str:
    """Name the offloaded callable, for its auto-hold label.

    Reads the callable's own ``__qualname__`` rather than inspecting the caller's
    frame. All three ``run_in_thread`` entry points funnel into one
    implementation, so a frame walk would name an SDK wrapper on two of the
    three; and the callee is the half of the site an operator can act on anyway.

    Only ever a code identifier — never an argument — so no query, path,
    credential or customer value can reach a hold label.

    Each fallback exists because something real lacks a usable ``__qualname__``:
    ``functools.partial`` carries no name of its own (the wrapped callable
    does), a callable *instance* has one only on its class, and a ``Mock``
    fabricates a child mock for whatever attribute is asked of it, which would
    otherwise put a repr into a label.
    """
    target: Any = func
    while isinstance(target, functools.partial):
        target = target.func
    for attribute in ("__qualname__", "__name__"):
        name = getattr(target, attribute, None)
        if isinstance(name, str) and name:
            return name
    return type(target).__name__


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
    - The offload is automatically wrapped in an **unbounded** progress hold
      (ADR-0018), so the stall watchdog never accuses a legitimately long
      blocking call of stalling. Nothing to do at the call site, and nothing
      about a long blocking call behaves differently than it did before the
      watchdog existed. The SDK does not invent a duration for somebody else's
      blocking call, so the watchdog is inactive for the call's whole duration
      and the activity's duration bound is the only thing still holding it.
      If you can say how long you would let this one call run before you would
      rather it failed, declare it — wrap the offload in
      ``holding_progress(label, timeout=...)``
      (:func:`application_sdk.execution.progress.holding_progress`) and a wedged
      call is caught at ``timeout`` + the no-progress budget instead of at the
      backstop.

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
    # Auto-hold, unbounded (ADR-0018 → *Feeding the tracker*, mechanism 2).
    # `run_in_thread` is a mandatory seam, so this is the one place the SDK can
    # vouch for blocking work for free — and it must vouch without inventing a
    # duration. `func`'s own `timeout=` kwarg is not one: it is per-operation (a
    # `requests` read timeout bounds the gap between socket reads, a
    # `statement_timeout` bounds one statement of many), so a bound derived from
    # it is systematically *smaller* than the call's legitimate duration, and
    # the dominant shape — `run_in_thread(cursor.execute, sql)` — carries no
    # timeout at all. Any fallback tight enough to be useful would be tighter
    # than today's `start_to_close`, i.e. the upgrade would make legitimate long
    # blocking work fail sooner than before. So: unbounded, the watchdog is
    # inactive for the call's duration, the duration backstop owns it, and warn
    # mode reports every closed hold with its observed duration. That residual
    # is accepted and surfaced, not closed.
    #
    # The hold starts before the executor dispatch on purpose, so it also covers
    # time spent queued behind a saturated `sdk-blocking-` pool: that is quiet
    # time the attempt can do nothing about, and killing it is exactly the
    # false-kill this hold exists to prevent.
    #
    # The tracker is read once and both calls go to that same object. Re-reading
    # for the release would pick up whatever tracker the context has by then,
    # which for a hold spanning a context change releases the wrong deadline.
    tracker = current_progress_tracker()
    hold = tracker.enter_hold(
        _THREAD_HOLD_PREFIX + _offloaded_callable_name(func), None
    )
    try:
        return await loop.run_in_executor(
            _BLOCKING_EXECUTOR,
            functools.partial(ctx.run, functools.partial(func, *args, **kwargs)),
        )
    finally:
        # `finally`, not the success path: an offload that raises, and an
        # activity cancelled mid-offload, must still release *their own* token.
        # A leaked unbounded hold vouches for the rest of the attempt, which
        # turns one exception into a watchdog that never fires again.
        tracker.exit_hold(hold)


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
# Lazy and cached by width: callers passing the same max_workers share one
# ProcessPoolExecutor of that width, created on first use; distinct widths get
# distinct pools, so discarding one width's pool on a crash never disturbs
# another's. Processes that never need isolation never pay for a child.
#
# Concurrency: the default pool runs several children so concurrent best-effort
# callers decode in PARALLEL, not serialised behind one child. Isolation here is
# purely fault containment — NOT a serialisation crutch to dodge the msgspec
# 0.20.0 concurrent-decode segfault: that bug is same-process (a shared
# in-process decoder across threads); separate child processes don't share that
# state, and once msgspec 0.21.1 lands even same-process concurrent decode is
# safe. The default width is capped low because each spawn child re-imports the
# decode stack (pyatlan/msgspec), which costs memory under a pod limit. A caller
# that wants to bound — or serialise (max_workers=1) — its own work passes an
# explicit width. (A crash in any one child breaks the whole ProcessPoolExecutor
# — inherent to the executor — so concurrent callers on that width then see
# BrokenProcessPool; for best-effort work that is a benign skip.)
_DEFAULT_PROCESS_POOL_MAX_WORKERS = min(4, (os.cpu_count() or 1))
_PROCESS_EXECUTORS: dict[int, concurrent.futures.ProcessPoolExecutor] = {}
_PROCESS_EXECUTORS_LOCK = threading.Lock()


def _get_process_executor(max_workers: int) -> concurrent.futures.ProcessPoolExecutor:
    with _PROCESS_EXECUTORS_LOCK:
        executor = _PROCESS_EXECUTORS.get(max_workers)
        if executor is None:
            executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=max_workers,
                mp_context=multiprocessing.get_context("spawn"),
            )
            _PROCESS_EXECUTORS[max_workers] = executor
        return executor


def _discard_process_executor(max_workers: int) -> None:
    """Drop the width-``max_workers`` pool (and kill its children) so the next
    call at that width starts fresh."""
    with _PROCESS_EXECUTORS_LOCK:
        executor = _PROCESS_EXECUTORS.pop(max_workers, None)
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
    func: Callable[..., T],
    *args: Any,
    timeout: float | None = None,
    max_workers: int | None = None,
    **kwargs: Any,
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

    Like :func:`run_in_thread`, the call is automatically wrapped in a progress
    hold (ADR-0018) so an isolated call — which emits nothing at all until the
    child returns — is never read as a stall. Here the hold is *bounded* by
    ``timeout`` when one is given, because that number is already a wall-clock
    kill enforced below; ``timeout=None`` gives an unbounded hold, matching its
    "wait forever" semantics.

    Args:
        func: Module-level function to run in the child.
        timeout: Seconds to wait before killing the child and raising
            ``TimeoutError``. ``None`` waits forever.
        max_workers: Width of the (width-keyed) process pool this call runs on.
            ``None`` (default) uses ``min(4, cpu_count)`` so concurrent callers
            decode in parallel. Pass ``1`` to opt into sequential execution —
            all ``max_workers=1`` callers share a single child and queue behind
            one another; pass another integer to bound concurrency at a
            different width. Must be >= 1.

    Raises:
        BrokenProcessPool: The child died abnormally (native crash), or a
            concurrent caller on the same pool discarded it (timeout) while this
            call was in flight. That pool is discarded; the next call at this
            width gets a fresh child.
        TimeoutError: ``timeout`` elapsed. The child is killed and the pool
            discarded.
        ValueError: ``max_workers`` is < 1.
    """
    workers = _DEFAULT_PROCESS_POOL_MAX_WORKERS if max_workers is None else max_workers
    if workers < 1:
        raise ValueError(f"max_workers must be >= 1, got {workers}")
    loop = asyncio.get_running_loop()
    # Auto-hold, bounded by this call's own `timeout` (ADR-0018 → *Feeding the
    # tracker*, mechanism 2 — the same treatment as `run_in_thread`, since this
    # is the SDK's other offload seam and an isolated call emits nothing at all
    # until the child returns; the SDK's own default `timeout` for the upload
    # validation scan is 600s, well past any plausible no-progress budget).
    #
    # Bounded rather than unbounded because unlike `run_in_thread` there is a
    # real wall-clock number here and it belongs to *this* function, not to
    # `func`: the block below enforces `timeout` by killing the child and raising
    # `TimeoutError`, so it means precisely what `enter_hold`'s allowance means —
    # how long the caller would let this one operation run before it would rather
    # it failed. What ADR-0018 rejects is deriving a bound from the *callee's*
    # per-operation kwargs, which is a different number. `timeout=None` still
    # means an unbounded hold, matching `None`'s "wait forever" here.
    tracker = current_progress_tracker()
    hold = tracker.enter_hold(
        _ISOLATED_HOLD_PREFIX + _offloaded_callable_name(func), timeout
    )
    try:
        future = loop.run_in_executor(
            _get_process_executor(workers), functools.partial(func, *args, **kwargs)
        )
        if timeout is not None:
            # Not asyncio.wait_for: on timeout it cancels the future and then
            # waits for the cancellation to land — but a running executor call
            # cannot be cancelled, so wait_for would hang exactly when the child
            # hangs. asyncio.wait just stops waiting; we then kill the child
            # ourselves.
            done, _ = await asyncio.wait({future}, timeout=timeout)
            if not done:
                _discard_process_executor(workers)
                # The kill resolves the abandoned future with BrokenProcessPool;
                # consume it so asyncio never logs "exception was never
                # retrieved".
                future.add_done_callback(
                    lambda f: None if f.cancelled() else f.exception()
                )
                raise TimeoutError(f"run_fault_isolated timed out after {timeout}s")
        try:
            return await future
        except BrokenProcessPool:
            _discard_process_executor(workers)
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
    finally:
        # Every exit releases this call's own token: a crashed child, a timeout,
        # a foreign pool discard, a real cancellation. A leaked hold would vouch
        # for the rest of the attempt.
        tracker.exit_hold(hold)


async def run_best_effort(
    func: Callable[..., T],
    *args: Any,
    label: str,
    logger: AtlanLoggerAdapter,
    timeout: float | None = None,
    max_workers: int | None = None,
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
        max_workers: Pool width, forwarded to :func:`run_fault_isolated`. ``None``
            uses the default ``min(4, cpu_count)``; pass ``1`` to serialise this
            work.

    Returns:
        ``func``'s result, or ``None`` if the run crashed, timed out, or errored.
    """
    try:
        return await run_fault_isolated(
            func, *args, timeout=timeout, max_workers=max_workers, **kwargs
        )
    except BrokenProcessPool:
        logger.warning(
            "%s subprocess died or was discarded (a native fault in a "
            "dependency, or a concurrent call's timeout); continuing without it",
            label,
            exc_info=True,
        )
    except TimeoutError:
        logger.warning(
            "%s timed out after %ss; continuing without it",
            label,
            timeout,
            exc_info=True,
        )
    except Exception:  # noqa: BLE001 — best-effort work must never break the caller
        logger.warning("%s skipped due to an unexpected error", label, exc_info=True)
    return None
