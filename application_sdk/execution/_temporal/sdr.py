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

import os
import time
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
    from application_sdk.errors.categories import FailureCategory
    from application_sdk.errors.leaves import DependencyUnavailableError
    from application_sdk.errors.wire import FailureDetails
    from application_sdk.handler.context import bind_invocation_context
    from application_sdk.handler.contracts import (
        AuthInput,
        AuthOutput,
        HandlerCredential,
        MetadataInput,
        MetadataOutput,
        PreflightCheck,
        PreflightInput,
        PreflightOutput,
        PreflightStatus,
    )
    from application_sdk.infrastructure.context import get_infrastructure
    from application_sdk.observability.logger_adaptor import get_logger
    from application_sdk.storage.preflight import check_object_store_access

if TYPE_CHECKING:
    from application_sdk.handler.base import Handler

logger = get_logger(__name__)

# UI-facing check-row names for the object-store access probes appended to the
# SDR interactive preflight output.  "deployment" is the customer's own store;
# "upstream" is the Atlan upload proxy.
_OBJECT_STORE_CHECK_NAMES: dict[str, str] = {
    "deployment": "Object store access (deployment)",
    "upstream": "Object store access (Atlan upload)",
}


SDR_TEST_AUTH_ACTIVITY = "sdr:test_auth"
SDR_PREFLIGHT_ACTIVITY = "sdr:preflight_check"
SDR_FETCH_METADATA_ACTIVITY = "sdr:fetch_metadata"


def _env_seconds(name: str, default: int) -> int:
    """Read a positive int number of seconds from ``name``, falling back on default.

    Mirrors the helper in LM's ``sdr_handler_proxy`` so the two sides tune from
    the same env-var convention. A missing, non-integer, or non-positive value
    falls back to ``default`` rather than raising — a ``0`` or negative timeout
    would otherwise flow into ``timedelta`` and be rejected by Temporal at
    activity-schedule time (a failure the inverted-pair warning below would not
    catch). Fallbacks on a *set* value are logged so the misconfig is visible.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    # Log the fallback outside the except so a routine bad-config value doesn't
    # attach a stack trace (the parse error carries no useful traceback here).
    if value is None:
        logger.warning(
            "SDR timeout env var %s=%r is not an integer; using default %ds.",
            name,
            raw,
            default,
        )
        return default
    if value < 1:
        logger.warning(
            "SDR timeout env var %s=%d is non-positive; using default %ds.",
            name,
            value,
            default,
        )
        return default
    return value


# UI-facing wall-clock caps. schedule_to_close is the only timeout that ticks
# even when no worker is polling the queue — without it, a UI request to an
# offline SDR worker would hang forever. start_to_close still bounds in-flight
# execution once a worker picks the activity up.
#
# These defaults are deliberately generous: SDR handlers resolve credentials
# from a customer-side secret store (Dapr / K8s / cloud secret manager) which
# can be slow to respond, and the source check itself runs over the customer's
# network. Each cap is env-tunable (ATLAN_SDR_{AUTH,PREFLIGHT,METADATA}_{SCHEDULE,
# START}_TO_CLOSE_SECONDS — ATLAN_ prefixed per ADR-0009, matching the sibling
# ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS) so a deployment fronting an especially slow
# store can raise them without an SDK release. Invariant: start_to_close <
# schedule_to_close in each pair, so at least one retry attempt fits inside the
# schedule cap; an inverted override is warned about at module load (below).
# Computed once at module load; the LM proxy's own WorkflowExecutionTimeout
# backstops sit just above these schedule_to_close values.
#
# NOTE: deliberately NO heartbeat_timeout. These activities run a single
# handler call (test_auth / preflight_check / fetch_metadata) and never call
# activity.heartbeat(), so setting a heartbeat_timeout would hard-cap runtime at
# that interval and fail any real source check that runs longer than it —
# start_to_close is the correct in-flight bound here.
_AUTH_SCHEDULE_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_AUTH_SCHEDULE_TO_CLOSE_SECONDS", 60)
)
_PREFLIGHT_SCHEDULE_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_PREFLIGHT_SCHEDULE_TO_CLOSE_SECONDS", 120)
)
_METADATA_SCHEDULE_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_METADATA_SCHEDULE_TO_CLOSE_SECONDS", 150)
)

_AUTH_START_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_AUTH_START_TO_CLOSE_SECONDS", 55)
)
_PREFLIGHT_START_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_PREFLIGHT_START_TO_CLOSE_SECONDS", 110)
)
_METADATA_START_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_METADATA_START_TO_CLOSE_SECONDS", 140)
)

# The start_to_close < schedule_to_close invariant holds for the defaults, but an
# operator can override either half independently. Warn (don't raise) at module
# load if any resolved pair is inverted: start_to_close >= schedule_to_close
# leaves no room for a retry attempt inside the schedule cap, so a misconfig is
# visible before the worker accepts work rather than surfacing as silent no-retry.
for _op, _env_label, _start, _schedule in (
    ("test_auth", "AUTH", _AUTH_START_TO_CLOSE, _AUTH_SCHEDULE_TO_CLOSE),
    (
        "preflight_check",
        "PREFLIGHT",
        _PREFLIGHT_START_TO_CLOSE,
        _PREFLIGHT_SCHEDULE_TO_CLOSE,
    ),
    (
        "fetch_metadata",
        "METADATA",
        _METADATA_START_TO_CLOSE,
        _METADATA_SCHEDULE_TO_CLOSE,
    ),
):
    if _start >= _schedule:
        logger.warning(
            "SDR %s: start_to_close (%.0fs) >= schedule_to_close (%.0fs); the "
            "schedule cap leaves no room for a retry attempt. Check the "
            "ATLAN_SDR_%s_{START,SCHEDULE}_TO_CLOSE_SECONDS overrides.",
            _op,
            _start.total_seconds(),
            _schedule.total_seconds(),
            _env_label,
        )

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
    agent_json: AgentCredentialSpec,
) -> list[HandlerCredential]:
    """Resolve an SDR ``agent_json`` *reference* to concrete credentials.

    SDR (customer-infra) connectors receive their credential as an agent-json
    reference — the real secret lives in the customer's Dapr / K8s secret store
    and only the worker can dereference it (``secret-path``). Mirrors the
    injected preflight gate's resolution: the caller only invokes this for a
    *populated* spec (``is_populated()``), matching the population gate in
    :meth:`CredentialRef.resolve`, so an empty spec never resolves to a bundle of
    empty strings. The already-parsed :class:`AgentCredentialSpec` (validated at
    the input parse boundary) is wrapped in a :class:`CredentialRef` and resolved
    against the worker's secret store *before* the handler runs. The resolved values are returned as the same
    v3 ``[{key, value}]`` :class:`HandlerCredential` list the handler consumes on
    the HTTP path, so ``input.credentials`` round-trips identically regardless of
    how the credential arrived.

    Raises:
        DependencyUnavailableError: A reference is present but no secret store is
            available to dereference it — an infra failure, not an empty-credential
            state. Raising (rather than calling the handler with empty creds)
            avoids misattributing the failure as an auth error.
    """
    ref = CredentialRef(agent_spec=agent_json)
    infra = get_infrastructure()
    secret_store = infra.secret_store if infra is not None else None
    if secret_store is None:
        raise DependencyUnavailableError(
            message="No secret store available to resolve SDR agent credentials",
            service="secret_store",
        )
    raw = await CredentialResolver(secret_store).resolve_raw(ref) or {}
    return HandlerCredential.list_from_raw(raw)


async def _append_object_store_checks(output: PreflightOutput) -> None:
    """Fold SDR object-store access probes into a handler's ``PreflightOutput``.

    Runs the customer object-store access check (deployment store + upstream
    Atlan upload proxy) and appends one ``PreflightCheck`` per probed store to
    ``output.checks`` so they render as UI check rows alongside the handler's own
    source/credential checks.  A failed probe carries a typed ``FailureDetails``
    (``DEPENDENCY_UNAVAILABLE`` / ``OBJECT_STORE_ACCESS``) so the message and
    remediation hint surface in the UI.

    No-op when not in SDR mode (``check_object_store_access`` returns ``[]`` when
    ``ENABLE_ATLAN_UPLOAD`` is falsy).  If any object-store check fails and the
    handler reported ``READY``, the verdict is downgraded to ``NOT_READY``; an
    already ``NOT_READY``/``PARTIAL`` verdict is left untouched.

    Never raises — any unexpected error is logged and the handler's own result is
    returned unchanged.
    """
    try:
        start = time.monotonic()
        results = await check_object_store_access(get_infrastructure())
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if not results:
            return

        any_failed = False
        for result in results:
            name = _OBJECT_STORE_CHECK_NAMES.get(
                result.label, f"Object store access ({result.label})"
            )
            error: FailureDetails | None = None
            if not result.passed:
                any_failed = True
                error = FailureDetails(
                    category=FailureCategory.DEPENDENCY_UNAVAILABLE,
                    code="OBJECT_STORE_ACCESS",
                    retryable=False,
                    message=result.message,
                    suggested_action=result.hint,
                )
            output.checks.append(
                PreflightCheck(
                    name=name,
                    passed=result.passed,
                    message=result.message,
                    error=error,
                )
            )

        output.total_duration_ms += elapsed_ms
        if any_failed and output.status == PreflightStatus.READY:
            output.status = PreflightStatus.NOT_READY
    except Exception:
        # Must never break the handler's own preflight result.
        logger.warning(
            "SDR preflight: object-store access check augmentation failed; "
            "leaving handler result unchanged",
            exc_info=True,
        )


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
        if input.agent_json is not None and input.agent_json.is_populated():
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            return await binding.handler.test_auth(input)

    @activity.defn(name=SDR_PREFLIGHT_ACTIVITY)
    async def preflight_check(input: PreflightInput) -> PreflightOutput:
        if input.agent_json is not None and input.agent_json.is_populated():
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            output = await binding.handler.preflight_check(input)
        # SDR-only: fold the customer object-store access check into the
        # interactive preflight so it shows up as a UI check row. Non-raising.
        await _append_object_store_checks(output)
        return output

    @activity.defn(name=SDR_FETCH_METADATA_ACTIVITY)
    async def fetch_metadata(input: MetadataInput) -> MetadataOutput:
        if input.agent_json is not None and input.agent_json.is_populated():
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            return await binding.handler.fetch_metadata(input)

    return [test_auth, preflight_check, fetch_metadata]
