"""Sizing telemetry at the interceptor, not a decorator: a decorator is skipped by
exactly the teams whose data is needed. Ships off; allow-list only.
"""

from __future__ import annotations

import os
import time
from typing import Any

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from application_sdk.observability.cgroup import track_container_usage
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.sizing import SizingObservation, record_observation
from application_sdk.observability.sizing_census import CENSUS
from application_sdk.observability.sizing_inputs import (
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
        concurrency_max = concurrency_now
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
            # peak(), not leave(): the slot is still held, and leaving early
            # would undercount concurrency for everything else running.
            concurrency_max = CENSUS.peak(token)
            # After the ``async with`` exits: the tracker fills the peak and CPU
            # deltas in its own ``finally``, so reading inside would record nothing.
            if trace is not None:
                duration_s = (time.monotonic_ns() - start_ns) / 1_000_000_000
                # Read after the activity, once readers have reported. args[1] is
                # the Input (args[0] is TaskContext).
                input_size = describe_inputs(
                    input.args[1] if len(input.args) > 1 else None
                )
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
            end_collection()


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
