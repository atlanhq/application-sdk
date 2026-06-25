"""Temporal interceptor that scopes the lineage-tracker ContextVar per activity.

Unlike :class:`~application_sdk.execution._temporal.interceptors.outputs.OutputInterceptor`,
this interceptor does NO aggregation. Cross-activity / cross-pod aggregation
cannot use process-local memory (activities can run on different pods), so the
reduce is a dedicated object-store-backed finalize step (added in a later PR),
not a workflow-exit merge.

The interceptor's only job: reset the lineage-tracker ContextVar at the start of
each activity (and clear it afterward) so a tracker created in one activity never
leaks into the next task that reuses the thread.
"""

from __future__ import annotations

from typing import Any

from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from application_sdk.observability.lineage.context import reset_lineage_tracker
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class LineageActivityInboundInterceptor(ActivityInboundInterceptor):
    """Resets the lineage-tracker ContextVar around each activity execution."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        reset_lineage_tracker()
        try:
            return await super().execute_activity(input)
        finally:
            # Never let a tracker leak into the next task on this thread.
            reset_lineage_tracker()


class LineageObservabilityInterceptor(Interceptor):
    """Top-level interceptor wiring lineage-tracker context scoping into activities."""

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return LineageActivityInboundInterceptor(super().intercept_activity(next))
