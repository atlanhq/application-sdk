"""Worker liveness interceptor.

Records worker progress so the ``/live`` health probe can reflect real activity
instead of always returning healthy. A single ``record`` callback is fired on:

* the start of every activity execution, and
* every ``activity.heartbeat()`` call.

Recording on heartbeat (not only on execution boundaries) keeps the liveness
timestamp fresh throughout a long-running activity — without it, a legitimate
multi-hour activity would look stalled to any timestamp-based liveness window.

This interceptor never observes the poll loop directly (Temporal exposes no
poll hook to Python), so a timestamp-based window is only a proxy for progress
and false-positives on idle queues. The authoritative, false-positive-free
liveness signal remains "is the worker run loop still alive?", enforced
separately in ``WorkerHealthServer.check_live`` via a run-task probe; see
``ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS`` for the opt-in window.
"""

from __future__ import annotations

from typing import Any, Callable

from temporalio.worker import (
    ActivityInboundInterceptor,
    ActivityOutboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)


class _LivenessActivityOutboundInterceptor(ActivityOutboundInterceptor):
    def __init__(
        self, next: ActivityOutboundInterceptor, record: Callable[[], None]
    ) -> None:
        super().__init__(next)
        self._record = record

    def heartbeat(self, *details: Any) -> None:
        self._record()
        self.next.heartbeat(*details)


class _LivenessActivityInboundInterceptor(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, record: Callable[[], None]
    ) -> None:
        super().__init__(next)
        self._record = record

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        self.next.init(_LivenessActivityOutboundInterceptor(outbound, self._record))

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self._record()
        return await self.next.execute_activity(input)


class LivenessInterceptor(Interceptor):
    """Fires a ``record`` callback on activity execution and every heartbeat.

    SDK-internal — instantiated by ``create_worker`` when an ``on_activity``
    callback is supplied (main entry point wires it to
    ``WorkerHealthServer.record_activity``). Not part of the user-facing
    interceptor set and not subject to the duplicate-builtin guard.
    """

    def __init__(self, record: Callable[[], None]) -> None:
        self._record = record

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _LivenessActivityInboundInterceptor(next, self._record)
