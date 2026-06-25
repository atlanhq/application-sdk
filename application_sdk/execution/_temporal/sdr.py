"""SDR (Self Deployed Runtime) Temporal workflows for handler operations.

Exposes the three handler operations -- ``test_auth``, ``preflight_check``,
``fetch_metadata`` -- as durable, retryable Temporal workflows in addition to
the HTTP endpoints already served by :mod:`application_sdk.handler.service`.

The workflows are generic and registered once per worker.  At worker
assembly time, :func:`build_sdr_activities` binds the concrete Handler
instance into three activity closures; the workflows reference those
activities by name.

SDR is enabled by default. When the worker is started without a Handler, the
SDK binds :class:`~application_sdk.handler.base.DefaultHandler` (no-op
canonical success) so the activities — including the injected preflight gate
(``sdr:preflight_gate``) — are always registered and dispatchable.
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
    from application_sdk.credentials.ref import CredentialRef, legacy_credential_ref
    from application_sdk.credentials.resolver import CredentialResolver
    from application_sdk.handler.context import HandlerContext, bind_handler_context
    from application_sdk.handler.contracts import (
        AuthInput,
        AuthOutput,
        HandlerCredential,
        MetadataInput,
        MetadataOutput,
        PreflightGateInput,
        PreflightInput,
        PreflightOutput,
    )
    from application_sdk.infrastructure.context import get_infrastructure
    from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from application_sdk.handler.base import Handler


SDR_TEST_AUTH_ACTIVITY = "sdr:test_auth"
SDR_PREFLIGHT_ACTIVITY = "sdr:preflight_check"
SDR_FETCH_METADATA_ACTIVITY = "sdr:fetch_metadata"
# Injected, in-workflow gate. Distinct from sdr:preflight_check (the HTTP/UI
# durable wrapper): the gate resolves credentials from references *inside* the
# activity, then dispatches the same handler.preflight_check.
SDR_PREFLIGHT_GATE_ACTIVITY = "sdr:preflight_gate"


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

# Injected gate (sdr:preflight_gate) dispatched from the extraction workflow's
# _run. Mirrors the preflight caps: bounded so a slow/unreachable source can't
# stall extraction start indefinitely.
_GATE_SCHEDULE_TO_CLOSE = timedelta(seconds=60)
_GATE_START_TO_CLOSE = timedelta(seconds=55)
_GATE_RETRY = RetryPolicy(maximum_attempts=2, backoff_coefficient=2)


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


SDR_WORKFLOWS: tuple[type, ...] = (
    SdrTestAuthWorkflow,
    SdrPreflightCheckWorkflow,
    SdrFetchMetadataWorkflow,
)


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
    credentials (and the worker's secret store, if any), binds it via
    ``bind_handler_context`` (ContextVar-backed) for the duration of the
    call, ensuring concurrent activities on a shared handler cannot
    overwrite each other's context.

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

    @activity.defn(name=SDR_PREFLIGHT_GATE_ACTIVITY)
    async def preflight_gate(input: PreflightGateInput) -> PreflightOutput:
        # Injected gate: the deterministic workflow passes only references, so
        # resolution happens here (mirrors templates/sql_app.py _init_sql_client),
        # then dispatches the same handler.preflight_check as the HTTP path.
        ref = _resolve_gate_ref(input)
        credentials: list[HandlerCredential] = []
        if ref is not None:
            infra = get_infrastructure()
            secret_store = infra.secret_store if infra is not None else None
            if secret_store is not None:
                raw = await CredentialResolver(secret_store).resolve_raw(ref) or {}
                credentials = HandlerCredential.list_from_raw(raw)
        preflight_input = PreflightInput(
            credentials=credentials,
            entrypoint=input.entrypoint,
            metadata=input.metadata,
        )
        with _bind_context(binding, credentials):
            result = await binding.handler.preflight_check(preflight_input)
        # Verdict record for debuggability: the canonical status the gate keys
        # on, plus the raw handler status when they differ (legacy folds).
        logger.info(
            "Preflight gate verdict: %s (entrypoint=%s, handler_status=%s, checks=%d)",
            result.canonical_status().value,
            input.entrypoint or "<implicit>",
            result.status.value,
            len(result.checks),
        )
        return result

    return [test_auth, preflight_check, fetch_metadata, preflight_gate]


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _resolve_gate_ref(input: PreflightGateInput) -> CredentialRef | None:
    """Resolve the credential reference for the injected gate.

    Mirrors the extraction path's ``_resolve_credential_ref``: prefer an
    already-built ``credential_ref``, else derive one via ``CredentialRef.resolve``
    (handles direct GUID + agent modes), falling back to ``legacy_credential_ref``.
    Returns ``None`` when the input carries no credential routing at all — the
    handler then runs with empty credentials (e.g. the no-op DefaultHandler).
    """
    if input.credential_ref is not None:
        return input.credential_ref
    if not (input.credential_guid or input.agent_json):
        return None
    try:
        return CredentialRef.resolve(input)
    except (ValueError, TypeError):
        if input.credential_guid:
            return legacy_credential_ref(input.credential_guid)
        return None


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
