"""Activity resource-sizing telemetry, collected at the interceptor.

An interceptor, not a decorator: ``create_worker`` already attaches SDK
interceptors to every activity in every v3 app, whereas a decorator would be
skipped by exactly the teams whose data is most needed. It also reads nothing from
the activity's signature, so it works for tasks the SDK has never seen.

A *sibling* of ``MetricsInterceptor``, not an extension — this one is gated, and
the App Vitals path should not gain a background task as a side effect.

Cost: ~4 file reads per activity plus 2 per poll tick, and no RPC. Unlike AE's
``report_memory_pressure``, whose per-tick heartbeat is a network call, this only
has to reach the local process — which is what makes a 1s default affordable.

Ships off, and measures only the activities named in the allow-list.
"""

from __future__ import annotations

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

logger = get_logger(__name__)


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
        """Whether this activity is on the allow-list.

        Matches the bare task name as well as the qualified one. A v3 activity
        registers as ``"{app}:{task}"``, but an author reading ``@task async def
        merge`` writes ``merge`` — and requiring the qualified form would silently
        collect nothing while the config looked correct.
        """
        if WILDCARD in self._activities:
            return True
        if activity_type in self._activities:
            return True
        return activity_type.rpartition(":")[2] in self._activities

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        # Filter before the tracker, so an unselected activity costs a set lookup
        # rather than the tracker's setup on every activity in the app.
        if not self._selected(info.activity_type or ""):
            return await self.next.execute_activity(input)

        start_ns = time.monotonic_ns()
        outcome = "OK"
        # Bound first so the outer ``finally`` cannot NameError if the tracker
        # never yields.
        trace = None
        try:
            async with track_container_usage(
                poll_interval_seconds=self._poll_interval_seconds
            ) as trace:
                return await self.next.execute_activity(input)
        # conformance: ignore[E004] measurement wrapper; re-raises immediately
        # after tagging the outcome for the record.
        except BaseException:
            outcome = "ERROR"
            raise
        finally:
            # After the ``async with`` exits: the tracker fills the peak and CPU
            # deltas in its own ``finally``, so reading inside would record nothing.
            if trace is not None:
                duration_s = (time.monotonic_ns() - start_ns) / 1_000_000_000
                record_observation(
                    SizingObservation.from_trace(
                        trace,
                        activity_type=info.activity_type or "",
                        task_queue=info.task_queue or "",
                        workflow_type=info.workflow_type or "",
                        attempt=info.attempt,
                        outcome=outcome,
                        duration_seconds=duration_s,
                    )
                )


class SizingTelemetryInterceptor(Interceptor):
    """Measures what each activity execution consumed.

    Per execution: ``activity.sizing.peak_memory_mib``, ``peak_memory_fraction``,
    ``cpu_throttled_fraction`` and ``mean_cpu_cores``, plus one structured
    ``activity_sizing_observation`` log line for offline tier fitting.

    ``activities`` is an opt-in allow-list; empty measures nothing, so being
    attached is never on its own enough to make this do work.

    Not exported and not accepted via ``create_worker(interceptors=...)``, which
    makes double-registration — two pollers per activity — impossible by
    construction rather than by a guard list.
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
