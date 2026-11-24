"""Run-shaping worker flags, pinned on the run so replay follows history.

Some worker settings change which commands a workflow emits: the event
interceptor's ``publish_event`` activities, and ``App.on_complete``'s cleanup
activities. If such a setting were read from the pod's environment on every
activation, a run started on a pod with one value and replayed on a pod with
another under the same build ID (a values-only rollout, or no
``ATLAN_APP_BUILD_ID``) would emit different commands and fail with a
nondeterminism error.

So each flag is decided once per run, in the run's first workflow task, and
recorded in history when it takes its *non-default* value:

* Live (not replaying): the worker's value, read once at worker start, decides.
  An off value records a ``workflow.patched`` marker.
* Replaying: the marker's presence decides. The pod's value is not consulted.

Recording the non-default keeps every run started under the defaults — every
production run before this module existed — replaying unchanged: no marker
reads as "on".

:class:`RunFlagsInterceptor` does the pinning and must sit ahead of every
interceptor that reads the result (the worker puts it first).
:func:`pinned_off` is the same decision for one marker, used directly where a
flag is decided at the point of use (the eviction cap in
:mod:`~application_sdk.execution._temporal.eviction_retry`).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from temporalio import workflow
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.common._env import env_bool

CLEANUP_ENV = "APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR"
"""Env var gating ``App.on_complete()``'s end-of-run cleanup."""

EVENTS_OFF_MARKER = "atlan-sdk/run-flag/events-off"
"""Recorded when a run starts with the event interceptor switched off."""

CLEANUP_OFF_MARKER = "atlan-sdk/run-flag/cleanup-off"
"""Recorded when a run starts with end-of-run cleanup switched off."""


@dataclass(frozen=True)
class RunFlags:
    """The run-shaping flags one run was pinned to."""

    events: bool
    cleanup: bool


_RUN_FLAGS: ContextVar[RunFlags | None] = ContextVar("atlan_run_flags", default=None)


def pinned_off(marker: str, live_enabled: bool) -> bool:
    """Whether the flag behind ``marker`` is off for this run.

    Must be called from workflow code. On replay the answer is whether
    ``marker`` is in history; live it is ``not live_enabled``, and an off value
    records ``marker`` so a later replay reads the same answer whatever the
    replaying pod's value is.
    """
    if workflow.unsafe.is_replaying_history_events():
        return workflow.patched(marker)
    if live_enabled:
        return False
    workflow.patched(marker)
    return True


def current_run_flags() -> RunFlags | None:
    """The flags :class:`RunFlagsInterceptor` pinned for the running workflow.

    ``None`` outside a workflow, and inside one run by a worker that did not
    register the interceptor.
    """
    return _RUN_FLAGS.get()


def cleanup_enabled() -> bool:
    """Whether ``App.on_complete()`` should run end-of-run cleanup.

    Inside a workflow run by an SDK worker, the run's pinned value. Otherwise
    (a direct ``on_complete()`` call, or a worker built without
    :class:`RunFlagsInterceptor`) the environment, defaulting to on.
    """
    flags = _RUN_FLAGS.get()
    if flags is not None:
        return flags.cleanup
    return env_bool(CLEANUP_ENV, True)


class RunFlagsInterceptor(Interceptor):
    """Pins the run-shaping flags at each run's first workflow task.

    Constructed once per worker with the worker's values, so the environment is
    read at worker start and never inside workflow code.
    """

    def __init__(self, *, events: bool, cleanup: bool) -> None:
        live = RunFlags(events=events, cleanup=cleanup)

        class _Inbound(_RunFlagsWorkflowInbound):
            _live = live

        self._inbound_cls: type[WorkflowInboundInterceptor] = _Inbound

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return self._inbound_cls


class _RunFlagsWorkflowInbound(WorkflowInboundInterceptor):
    _live: RunFlags

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> object:
        # Both decisions happen before any other command, in a fixed order,
        # so the markers sit ahead of the first publish_event.
        pinned = RunFlags(
            events=not pinned_off(EVENTS_OFF_MARKER, self._live.events),
            cleanup=not pinned_off(CLEANUP_OFF_MARKER, self._live.cleanup),
        )
        token = _RUN_FLAGS.set(pinned)
        try:
            return await super().execute_workflow(input)
        finally:
            _RUN_FLAGS.reset(token)
