"""Activity resource-sizing telemetry, collected at the interceptor.

Why an interceptor and not a decorator. A decorator would have to be applied by
every app author to every task method — 44 v3 apps, and the ones that forget are
exactly the ones with no sizing data, so the dataset would be biased towards
teams who already care about resource usage. ``create_worker`` already attaches
the SDK's interceptors to every activity in every v3 app, so the interceptor is
the only hook that is uniform by construction. It also needs nothing from the
activity's signature, which matters because it has to work for tasks the SDK has
never seen.

A **sibling** of ``MetricsInterceptor`` rather than an extension of it, for two
reasons: this one is gated and that one is unconditional, and the App Vitals
metrics path should not gain a background task and a set of cgroup reads as a
side effect of a sizing rollout.

**Cost.** Two small file reads per poll tick and roughly four per activity, with
no RPC. Deliberately unlike AE's ``report_memory_pressure``, whose per-tick
``activity.heartbeat()`` is a network call — that one is a safety device whose
readings must reach the workflow, this one only has to reach the local process.
That difference is what makes a 1-second default defensible fleet-wide.

Ships **off**. Collection is enabled per tenant by
``APPLICATION_SDK_ENABLE_SIZING_TELEMETRY``, so a version bump alone changes
nothing.
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


class _SizingActivityInboundInterceptor(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, poll_interval_seconds: float
    ) -> None:
        super().__init__(next)
        self._poll_interval_seconds = poll_interval_seconds

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        start_ns = time.monotonic_ns()
        outcome = "OK"
        # Bound before the ``async with`` so the outer ``finally`` cannot raise
        # NameError on the one path where the tracker never yields.
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
            # Read the trace *after* the ``async with`` has exited. The tracker
            # fills in the peak watermark and the CPU deltas in its own
            # ``finally``, so reading inside the block would record a trace that
            # is still empty.
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
    """Measures what each activity execution actually consumed.

    Emits, per activity execution:

    * ``activity.sizing.peak_memory_mib`` — peak cgroup memory
    * ``activity.sizing.peak_memory_fraction`` — the utilisation view
    * ``activity.sizing.cpu_throttled_fraction`` — CPU starvation
    * ``activity.sizing.mean_cpu_cores`` — CPU consumed per wall second

    plus one structured ``activity_sizing_observation`` log line carrying the
    full record for offline tier fitting.

    Not exported from the ``interceptors`` package and not accepted via
    ``create_worker(interceptors=...)``: it is wired only by ``create_worker``
    when collection is enabled. Keeping it un-exported is what makes
    double-registration — and therefore two pollers per activity — impossible by
    construction rather than by a guard list.
    """

    def __init__(self, poll_interval_seconds: float = 1.0) -> None:
        self._poll_interval_seconds = poll_interval_seconds

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _SizingActivityInboundInterceptor(next, self._poll_interval_seconds)
