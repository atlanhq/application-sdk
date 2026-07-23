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
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.resolver import CredentialResolver
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.errors.leaves import DependencyUnavailableError
    from application_sdk.handler.context import bind_invocation_context
    from application_sdk.handler.contracts import (
        AuthInput,
        AuthOutput,
        HandlerCredential,
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


SDR_WORKFLOWS: tuple[type, ...] = (
    SdrTestAuthWorkflow,
    SdrPreflightCheckWorkflow,
    SdrFetchMetadataWorkflow,
)


@dataclass
class _SdrBinding:
    handler: Handler
    app_name: str


async def _resolve_agent_credentials(
    agent_json: dict[str, Any],
) -> list[HandlerCredential]:
    """Resolve an SDR ``agent_json`` *reference* to concrete credentials.

    SDR (customer-infra) connectors receive their credential as an agent-json
    reference — the real secret lives in the customer's Dapr / K8s secret store
    and only the worker can dereference it (``secret-path``). Mirrors the
    injected preflight gate's resolution exactly: build an
    :class:`AgentCredentialSpec` from the raw ``agent_json``, wrap it in a
    :class:`CredentialRef`, and resolve it against the worker's secret store
    *before* the handler runs. The resolved values are returned as the same v3
    ``[{key, value}]`` :class:`HandlerCredential` list the handler consumes on
    the HTTP path, so ``input.credentials`` round-trips identically regardless of
    how the credential arrived.

    Raises:
        DependencyUnavailableError: A reference is present but no secret store is
            available to dereference it — an infra failure, not an empty-credential
            state. Raising (rather than calling the handler with empty creds)
            avoids misattributing the failure as an auth error.
    """
    spec = AgentCredentialSpec.model_validate(agent_json)
    ref = CredentialRef(agent_spec=spec)
    infra = get_infrastructure()
    secret_store = infra.secret_store if infra is not None else None
    if secret_store is None:
        raise DependencyUnavailableError(
            message="No secret store available to resolve SDR agent credentials",
            service="secret_store",
        )
    raw = await CredentialResolver(secret_store).resolve_raw(ref) or {}
    return HandlerCredential.list_from_raw(raw)


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

    When the input carries an ``agent_json`` reference (SDR / customer-infra
    connections, where the credential is a ``secret-path`` reference the worker
    dereferences via its own secret store), the activity resolves it to concrete
    ``HandlerCredential``s *before* binding the context — mirroring the injected
    preflight gate. Inputs without ``agent_json`` (the HTTP / direct path and
    non-SDR callers) pass their already-resolved ``credentials`` through
    unchanged.

    Activities registered here have closure access to ``handler``; they
    are resolved by name from the workflows in :data:`SDR_WORKFLOWS`.
    """
    binding = _SdrBinding(handler=handler, app_name=app_name)

    @activity.defn(name=SDR_TEST_AUTH_ACTIVITY)
    async def test_auth(input: AuthInput) -> AuthOutput:
        if input.agent_json:
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            return await binding.handler.test_auth(input)

    @activity.defn(name=SDR_PREFLIGHT_ACTIVITY)
    async def preflight_check(input: PreflightInput) -> PreflightOutput:
        if input.agent_json:
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            return await binding.handler.preflight_check(input)

    @activity.defn(name=SDR_FETCH_METADATA_ACTIVITY)
    async def fetch_metadata(input: MetadataInput) -> MetadataOutput:
        if input.agent_json:
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            return await binding.handler.fetch_metadata(input)

    return [test_auth, preflight_check, fetch_metadata]
