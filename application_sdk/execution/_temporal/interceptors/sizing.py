"""Sizing telemetry at the interceptor, not a decorator: a decorator is skipped by
exactly the teams whose data is needed. Ships off; allow-list only.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from application_sdk._runtime.offload import blocking_executor, run_in_thread
from application_sdk.observability.cgroup import ContainerTrace, track_container_usage
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.sizing import SizingObservation, record_observation
from application_sdk.observability.sizing_census import CENSUS
from application_sdk.observability.sizing_inputs import (
    InputSize,
    begin_collection,
    describe_inputs,
    end_collection,
)

logger = get_logger(__name__)


def _pod_name() -> str | None:
    """Pod name — the join key for overlapping executions. Same source as the OTel
    resource attributes, so rows and metrics agree.
    """
    return os.environ.get("K8S_POD_NAME") or os.environ.get("HOSTNAME") or None


#: Most time the input walk after the activity returns may take. The walk is
#: capped at 10k files, but a slow volume can still be slow; past this, input
#: size is recorded as unknown. Never more than the attempt has left: see
#: ``_walk_budget_seconds``.
DESCRIBE_INPUTS_TIMEOUT_SECONDS = 10.0

#: Left unspent before the attempt's deadline, for completing the activity and
#: for clock skew between the worker and the Temporal server that set it.
_DEADLINE_MARGIN_SECONDS = 1.0

# The walk's own pool, not the shared ``run_in_thread`` one. A timed-out walk
# cannot be stopped, and one stat on an unresponsive mount can block for good:
# here that pins one sizing thread, never a slot unrelated offloads need. One
# worker, because ``_WALKS`` skips a walk while another is still running, so
# nothing queues behind a stuck one.
_SIZING_WALK_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="sdk-sizing-walk-"
)


class _WalkTracker:
    """Whether a walk is still running on its thread, even after its caller
    stopped waiting for it. Counted by the thread itself, so an abandoned walk
    stays visible until it really returns.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = 0

    def busy(self) -> bool:
        with self._lock:
            return self._running > 0

    def describe(self, input_data: Any) -> InputSize | None:
        with self._lock:
            self._running += 1
        try:
            return describe_inputs(input_data)
        finally:
            with self._lock:
                self._running -= 1


_WALKS = _WalkTracker()


def _utc(moment: datetime) -> datetime:
    """Temporal's times are UTC; treat a naive one as UTC rather than fail on it."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _walk_budget_seconds(info: Any, now: datetime) -> float:
    """Seconds the input walk may take, bounded by the attempt's own deadlines.

    The walk runs inside the attempt: running past ``start_to_close`` or
    ``schedule_to_close`` lets Temporal time the attempt out and retry work that
    already completed. The activity's auto-heartbeat has stopped by now, so the
    walk also stays within half the heartbeat timeout (the last beat may have
    been up to one interval ago). ``<= 0`` means there is no time to walk.
    """
    budget = DESCRIBE_INPUTS_TIMEOUT_SECONDS
    now = _utc(now)
    if info.start_to_close_timeout is not None:
        deadline = _utc(info.started_time) + info.start_to_close_timeout
        budget = min(
            budget, (deadline - now).total_seconds() - _DEADLINE_MARGIN_SECONDS
        )
    if info.schedule_to_close_timeout is not None:
        deadline = _utc(info.scheduled_time) + info.schedule_to_close_timeout
        budget = min(
            budget, (deadline - now).total_seconds() - _DEADLINE_MARGIN_SECONDS
        )
    if info.heartbeat_timeout is not None:
        budget = min(budget, info.heartbeat_timeout.total_seconds() / 2)
    return budget


#: Selects every activity. For a discovery pass on a test tenant, not production.
WILDCARD = "*"


class _SizingActivityInboundInterceptor(ActivityInboundInterceptor):
    def __init__(
        self,
        next: ActivityInboundInterceptor,
        poll_interval_seconds: float,
        activities: frozenset[str],
    ) -> None:
        super().__init__(next)
        self._poll_interval_seconds = poll_interval_seconds
        self._activities = activities

    def _selected(self, activity_type: str) -> bool:
        """Whether this activity is allow-listed. Matches the bare task name as
        well as ``"{app}:{task}"``, or config that looks right collects nothing.
        """
        if WILDCARD in self._activities:
            return True
        if activity_type in self._activities:
            return True
        return activity_type.rpartition(":")[2] in self._activities

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        # EVERY activity counts, selected or not: what invalidates attribution is
        # other work using the pod, not whether we were measuring it.
        token, concurrency_now = CENSUS.enter()
        try:
            if not self._selected(info.activity_type or ""):
                return await self.next.execute_activity(input)
            return await self._measured(input, info, token, concurrency_now)
        finally:
            CENSUS.leave(token)

    async def _measured(
        self,
        input: ExecuteActivityInput,
        info: Any,
        token: int,
        concurrency_now: int,
    ) -> Any:
        start_ns = time.monotonic_ns()
        # Wall-clock as well as monotonic: monotonic has no epoch, so it cannot be
        # compared across executions, and comparing windows is the whole point.
        started_at = time.time()
        outcome = "OK"
        # Created here so reads accumulate into it; without one, reporting is a
        # no-op, which keeps report_input_bytes() free to call from a hot path.
        begin_collection()
        # Bound first so the outer ``finally`` cannot NameError if the tracker
        # never yields.
        trace = None
        try:
            async with track_container_usage(
                poll_interval_seconds=self._poll_interval_seconds,
                # Only the sole occupant may reset the shared counter. Later
                # arrivals inflate this peak rather than corrupting it.
                allow_watermark_reset=concurrency_now <= 1,
            ) as trace:
                return await self.next.execute_activity(input)
        # conformance: ignore[E004] measurement wrapper; re-raises immediately
        # after tagging the outcome for the record.
        except BaseException:
            outcome = "ERROR"
            raise
        finally:
            # try/finally: whatever happens to the telemetry, the collection ends,
            # so a finished activity cannot leak into the next one.
            try:
                if trace is not None:
                    await self._record_safely(
                        input, info, token, trace, outcome, start_ns, started_at
                    )
            finally:
                end_collection()

    async def _record_safely(
        self,
        input: ExecuteActivityInput,
        info: Any,
        token: int,
        trace: ContainerTrace,
        outcome: str,
        start_ns: int,
        started_at: float,
    ) -> None:
        """Record one observation. Never raises, including on cancel.

        Runs in ``_measured``'s ``finally``, after the activity has finished:
        anything that escaped here would replace its result (or its real error),
        and Temporal would retry work whose side effects already ran. Telemetry
        is dropped instead.
        """
        try:
            # peak(), not leave(): the slot is still held, and leaving early
            # would undercount concurrency for everything else running.
            concurrency_max = CENSUS.peak(token)
            # After the ``async with`` exits: the tracker fills the peak and CPU
            # deltas in its own ``finally``, so reading inside would record nothing.
            duration_s = (time.monotonic_ns() - start_ns) / 1_000_000_000
            input_size = await self._describe_inputs_bounded(input, info)
            record_observation(
                SizingObservation.from_trace(
                    trace,
                    activity_type=info.activity_type or "",
                    task_queue=info.task_queue or "",
                    workflow_type=info.workflow_type or "",
                    attempt=info.attempt,
                    outcome=outcome,
                    duration_seconds=duration_s,
                    input_size=input_size,
                    started_at=started_at,
                    pod=_pod_name(),
                    concurrency_max=concurrency_max,
                )
            )
        # conformance: ignore[E004] telemetry after the activity finished; raising
        # would turn a completed activity into a retry.
        except asyncio.CancelledError:
            # A cancel that lands here (worker shutdown past its graceful
            # timeout) arrived after the activity finished. Absorb it so the
            # finished outcome is what Temporal sees; uncancel() so structured
            # concurrency (asyncio.timeout, TaskGroup) does not see a pending one.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            logger.warning(
                "Sizing telemetry cancelled after activity %s finished; "
                "observation dropped, activity outcome kept",
                info.activity_type,
                exc_info=True,
            )
        # conformance: ignore[E004] telemetry after the activity finished; raising
        # would turn a completed activity into a retry.
        except Exception:
            logger.warning(
                "Sizing telemetry failed for activity %s; "
                "observation dropped, activity outcome kept",
                info.activity_type,
                exc_info=True,
            )

    async def _describe_inputs_bounded(
        self, input: ExecuteActivityInput, info: Any
    ) -> InputSize | None:
        """Input size, or ``None`` (unknown) if there is no time or thread for it.

        args[1] is the Input (args[0] is TaskContext). In a thread: with no
        reported reads it walks every materialised FileReference, up to 10k files
        (FND-4084). The context is copied, so the collector begin_collection()
        set is still the one it reads.
        """
        input_data = input.args[1] if len(input.args) > 1 else None
        budget = _walk_budget_seconds(info, datetime.now(timezone.utc))
        if budget <= 0:
            logger.warning(
                "No time left before activity %s's deadline to size its input; "
                "recording the observation with input size unknown",
                info.activity_type,
            )
            return None
        if _WALKS.busy():
            logger.warning(
                "A previous input walk is still running (an unresponsive "
                "filesystem?); recording activity %s's observation with input "
                "size unknown",
                info.activity_type,
            )
            return None
        try:
            with blocking_executor(_SIZING_WALK_EXECUTOR):
                return await asyncio.wait_for(
                    run_in_thread(_WALKS.describe, input_data), timeout=budget
                )
        except TimeoutError:
            # The thread cannot be interrupted; it finishes on its own pool, and
            # later walks are skipped until it does.
            logger.warning(
                "Input sizing for activity %s exceeded %.1fs; "
                "recording the observation with input size unknown",
                info.activity_type,
                budget,
                exc_info=True,
            )
            return None


class SizingTelemetryInterceptor(Interceptor):
    """Measures what each execution consumed. Opt-in allow-list, and not accepted
    via ``create_worker(interceptors=...)`` — no double-registration by design.
    """

    def __init__(
        self,
        poll_interval_seconds: float = 1.0,
        activities: frozenset[str] = frozenset(),
    ) -> None:
        self._poll_interval_seconds = poll_interval_seconds
        self._activities = activities

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _SizingActivityInboundInterceptor(
            next, self._poll_interval_seconds, self._activities
        )
