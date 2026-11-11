"""Tell the event-loop stall watchdog which heartbeat timeouts are in play.

The watchdog in ``application_sdk._loop_stall_watchdog`` waits for the longest
heartbeat timeout among running activities before it ends a worker whose loop
has stopped, so it needs to know what is running.

``execute_activity`` runs on the event loop for every activity, including a
sync one (temporalio hands only the function body to the executor), and a
sync activity's heartbeats also travel through the loop, so every activity is
counted the same way and the registry is only ever touched on the loop thread.

SDK-internal: ``create_worker`` always adds it, and it is not re-exported.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from application_sdk._loop_stall_watchdog import (
    RunningHeartbeatTimeouts,
    running_heartbeat_timeouts,
)


class _LoopStallActivityInboundInterceptor(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, running: RunningHeartbeatTimeouts
    ) -> None:
        super().__init__(next)
        self._running = running

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        timeout = activity.info().heartbeat_timeout
        seconds = timeout.total_seconds() if timeout is not None else None
        with self._running.track(seconds):
            return await self.next.execute_activity(input)


class LoopStallInterceptor(Interceptor):
    """Records each running activity's heartbeat timeout for the watchdog."""

    def __init__(self, running: RunningHeartbeatTimeouts | None = None) -> None:
        self._running = running if running is not None else running_heartbeat_timeouts

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _LoopStallActivityInboundInterceptor(next, self._running)
