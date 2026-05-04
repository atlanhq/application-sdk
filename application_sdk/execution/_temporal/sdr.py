"""SDR (Self Deployed Runtime) Temporal workflows for handler operations.

Exposes the three handler operations -- ``test_auth``, ``preflight_check``,
``fetch_metadata`` -- as durable, retryable Temporal workflows in addition to
the HTTP endpoints already served by :mod:`application_sdk.handler.service`.

The workflows are generic and registered once per worker.  At worker
assembly time, :func:`build_sdr_activities` binds the concrete Handler
instance into three activity closures; the workflows reference those
activities by name.

SDR is enabled by default and silently skipped when the worker is started
without a Handler (see ``create_worker(handler=...)``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from application_sdk.handler.context import HandlerContext, bind_handler_context
    from application_sdk.handler.contracts import (
        AuthInput,
        AuthOutput,
        MetadataInput,
        MetadataOutput,
        PreflightInput,
        PreflightOutput,
    )
    from application_sdk.infrastructure.context import get_infrastructure

if TYPE_CHECKING:
    from application_sdk.handler.base import Handler


SDR_TEST_AUTH_ACTIVITY = "sdr:test_auth"
SDR_PREFLIGHT_ACTIVITY = "sdr:preflight_check"
SDR_FETCH_METADATA_ACTIVITY = "sdr:fetch_metadata"


# UI-facing wall-clock caps. schedule_to_close is the only timeout that ticks
# even when no worker is polling the queue — without it, a UI request to an
# offline SDR worker would hang forever. start_to_close still bounds in-flight
# execution once a worker picks the activity up.
_AUTH_SCHEDULE_TO_CLOSE = timedelta(seconds=30)
_PREFLIGHT_SCHEDULE_TO_CLOSE = timedelta(seconds=60)
_METADATA_SCHEDULE_TO_CLOSE = timedelta(seconds=90)

_DEFAULT_HEARTBEAT = timedelta(seconds=15)
_AUTH_START_TO_CLOSE = timedelta(seconds=25)
_PREFLIGHT_START_TO_CLOSE = timedelta(seconds=55)
_METADATA_START_TO_CLOSE = timedelta(seconds=85)

# test_auth: fail-fast. A wrong password should not retry; transient network
# errors get one extra attempt only because the user is sitting on the UI.
_AUTH_RETRY = RetryPolicy(maximum_attempts=1)
# preflight / fetch_metadata: brief retries against flaky sources, but bounded
# by schedule_to_close above.
_DEFAULT_RETRY = RetryPolicy(maximum_attempts=2, backoff_coefficient=2)


@workflow.defn(name="sdr:test_auth")
class SdrTestAuthWorkflow:
    """Durable wrapper around ``Handler.test_auth``."""

    @workflow.run
    async def run(self, input: AuthInput) -> AuthOutput:
        return await workflow.execute_activity(
            SDR_TEST_AUTH_ACTIVITY,
            input,
            retry_policy=_AUTH_RETRY,
            schedule_to_close_timeout=_AUTH_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_AUTH_START_TO_CLOSE,
            heartbeat_timeout=_DEFAULT_HEARTBEAT,
        )


@workflow.defn(name="sdr:preflight_check")
class SdrPreflightCheckWorkflow:
    """Durable wrapper around ``Handler.preflight_check``."""

    @workflow.run
    async def run(self, input: PreflightInput) -> PreflightOutput:
        return await workflow.execute_activity(
            SDR_PREFLIGHT_ACTIVITY,
            input,
            retry_policy=_DEFAULT_RETRY,
            schedule_to_close_timeout=_PREFLIGHT_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_PREFLIGHT_START_TO_CLOSE,
            heartbeat_timeout=_DEFAULT_HEARTBEAT,
        )


@workflow.defn(name="sdr:fetch_metadata")
class SdrFetchMetadataWorkflow:
    """Durable wrapper around ``Handler.fetch_metadata``."""

    @workflow.run
    async def run(self, input: MetadataInput) -> MetadataOutput:
        return await workflow.execute_activity(
            SDR_FETCH_METADATA_ACTIVITY,
            input,
            retry_policy=_DEFAULT_RETRY,
            schedule_to_close_timeout=_METADATA_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_METADATA_START_TO_CLOSE,
            heartbeat_timeout=_DEFAULT_HEARTBEAT,
        )


SDR_WORKFLOWS: list[type] = [
    SdrTestAuthWorkflow,
    SdrPreflightCheckWorkflow,
    SdrFetchMetadataWorkflow,
]


@dataclass
class _SdrBinding:
    handler: Handler
    app_name: str


def build_sdr_activities(
    handler: Handler,
    app_name: str,
) -> list[Callable[..., Awaitable[Any]]]:
    """Build three Temporal activity callables bound to a Handler instance.

    Each activity mirrors the dispatch done by ``handler/service.py`` for
    its HTTP counterpart: it builds a ``HandlerContext`` from the input's
    credentials (and the worker's secret store, if any), sets
    ``handler._context`` for the duration of the call, and clears it in
    ``finally``.

    Activities registered here have closure access to ``handler``; they
    are resolved by name from the workflows in :data:`SDR_WORKFLOWS`.
    """
    binding = _SdrBinding(handler=handler, app_name=app_name)

    @activity.defn(name=SDR_TEST_AUTH_ACTIVITY)
    async def test_auth(input: AuthInput) -> AuthOutput:
        with _bind_context(binding, input.credentials):
            return await binding.handler.test_auth(input)

    @activity.defn(name=SDR_PREFLIGHT_ACTIVITY)
    async def preflight_check(input: PreflightInput) -> PreflightOutput:
        with _bind_context(binding, input.credentials):
            return await binding.handler.preflight_check(input)

    @activity.defn(name=SDR_FETCH_METADATA_ACTIVITY)
    async def fetch_metadata(input: MetadataInput) -> MetadataOutput:
        with _bind_context(binding, input.credentials):
            return await binding.handler.fetch_metadata(input)

    return [test_auth, preflight_check, fetch_metadata]


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


@contextmanager
def _bind_context(binding: _SdrBinding, credentials: list[Any]):
    """Bind a per-invocation HandlerContext for the duration of the block.

    Uses bind_handler_context (ContextVar-backed) so concurrent SDR activities
    on a shared handler instance cannot overwrite each other's context.
    """
    infra = get_infrastructure()
    secret_store = infra.secret_store if infra is not None else None

    context = HandlerContext(
        app_name=binding.app_name,
        request_id=uuid4(),
        started_at=datetime.now(UTC),
        _credentials=list(credentials),
        _secret_store=secret_store,
    )
    with bind_handler_context(context):
        yield context
