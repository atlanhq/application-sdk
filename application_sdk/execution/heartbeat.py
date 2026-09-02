"""Heartbeat support for long-running tasks.

Two modes of heartbeating are supported:

1. Automatic (framework-managed): background task sends heartbeats at configured
   intervals — zero developer effort.
2. Manual (developer-controlled): developer calls heartbeat() with progress info
   for resume-on-retry support.

:func:`auto_heartbeat_loop` also runs the ADR-0018 stall watchdog on the same
tick, since both questions are asked once per interval.

This module is additionally the **app-facing path to the offload seam**. Its
four sanctioned primitives — ``run_in_thread``, ``submit_in_thread``,
``run_fault_isolated`` and ``run_best_effort`` — are implemented in
:mod:`application_sdk._runtime.offload` and re-exported here unchanged. They
moved to the substrate so every layer, ``storage/`` included, can import a
mandatory seam at module scope (ADR-0019); this remains the documented path for
app code, and SDK-internal callers import ``application_sdk._runtime.offload``
directly.
"""

import asyncio
import math
import os
import time
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool
from typing import Any, Protocol

from application_sdk._runtime.offload import (
    run_best_effort,
    run_fault_isolated,
    run_in_thread,
    submit_in_thread,
)
from application_sdk._runtime.progress import (
    ProgressTracker,
    current_progress_tracker,
    declared_hold_active,
)
from application_sdk.execution.progress import ProgressWatchdogMode
from application_sdk.execution.progress_telemetry import record_no_progress_gap
from application_sdk.execution.run_length import RunLengthWatch
from application_sdk.observability import (
    resource_sampler as _resource_sampler,  # module alias kept so tests can patch _resource_sampler.sample()
)
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter, get_logger
from application_sdk.observability.resource_sampler import parse_pod_memory_limit

logger = get_logger(__name__)

# Names this module never used itself but which were importable from it before the
# offload seam moved out (FND-316), because the pre-split file imported them for its
# own implementation. Nothing in the SDK or any consumer imports them from here
# today — but they resolved, so removing them would be a breaking change dressed up
# as a refactor. Kept so `from application_sdk.execution.heartbeat import <name>`
# never regresses; their real homes are `_runtime.progress`,
# `observability.logger_adaptor` and `concurrent.futures.process`. Pinned by
# `tests/unit/runtime/test_layering.py`.
__all__ = [
    "AtlanLoggerAdapter",
    "BrokenProcessPool",
    "HeartbeatController",
    "NoopHeartbeatController",
    "ProgressTracker",
    "ProgressWatchdogMode",
    "TemporalHeartbeatController",
    "auto_heartbeat_loop",
    "current_progress_tracker",
    "stop_heartbeat_task",
    "declared_hold_active",
    "record_no_progress_gap",
    "run_best_effort",
    "run_fault_isolated",
    "run_in_thread",
    "submit_in_thread",
]

_MEMORY_WARN_THRESHOLD = 0.80
_MEMORY_WARN_HYSTERESIS = (
    0.05  # re-arm only once ratio drops below threshold - hysteresis
)


async def stop_heartbeat_task(
    task: asyncio.Task,
    stop_event: asyncio.Event,
    task_name: str,
) -> None:
    """Stop an ``auto_heartbeat_loop`` task, letting nothing escape.

    Sets the loop's stop event, bounds the wait for a graceful exit, and
    cancels if the bound expires — then awaits the cancellation. Cleanup must
    never outrank the payload it follows: a ``finally`` that raised (a
    swallowed-then-re-raised ``CancelledError`` is a ``BaseException``) would
    replace the verdict or result the caller just computed.

    This function deliberately catches ``BaseException``: the task being
    cleaned up may swallow the cancel and re-raise it on await, and a stuck
    loop must not turn a completed activity into a failed one. The one
    ``BaseException`` that must NOT be contained is a cancellation aimed at
    the *caller* while it waits here — swallowing that would make a cancelled
    activity report completion. The two are separable:
    ``Task.cancelling()`` counts cancel requests made against the caller's own
    task, and the heartbeat task re-raising its internal ``CancelledError``
    never increments it.
    """

    def _caller_is_being_cancelled() -> bool:
        current = asyncio.current_task()
        return current is not None and current.cancelling() > 0

    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=1.0)
        return
    # conformance: ignore[E002] cleanup must never outrank the payload; a stuck loop is not the caller's failure
    except BaseException:  # noqa: S110 — the cancel below is the handling
        if _caller_is_being_cancelled():
            task.cancel()
            raise
    task.cancel()
    try:
        await task
    # conformance: ignore[E002] the cancelled task's BaseException is the cleanup ending, not an error to act on
    except BaseException:
        if _caller_is_being_cancelled():
            raise
        logger.debug(
            "Heartbeat task '%s' did not stop cleanly", task_name, exc_info=True
        )


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
    record_no_progress_gap(task_name, stalled, last_label, mode)

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
    run_length: RunLengthWatch | None = None,
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

    A ``run_length`` watch is a third such question, and the one the watchdog
    cannot answer: *has the whole run been going too long?* A run that dribbles
    progress re-arms the watchdog on every mark and stalls by no definition it
    has, so with the duration ceiling raised to a backstop the only thing left
    to bound it is a human — which needs an alert (ADR-0018 → *Bounding total
    time*). It rides this tick because the tick already exists; it throttles
    itself and reports nothing while the run is inside its SLA.

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
        run_length: This attempt's :class:`~application_sdk.execution.run_length.RunLengthWatch`.
            ``None`` — the default — makes the loop byte-identical to before:
            no run-length observation is made. Injected rather than built here
            because only the activity layer knows when the *run* started.
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

        # Ahead of the watchdog for the same reason the memory sample is: an
        # enforced stall returns out of the loop, and the tick that kills a
        # wedged attempt is exactly the tick on which "this run is also 30 hours
        # long" is worth having recorded. `observe()` swallows its own failures.
        if run_length is not None:
            run_length.observe()

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
