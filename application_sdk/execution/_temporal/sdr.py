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
    from application_sdk.credentials.agent import (
        SecretStoreCheckResult,
        check_secret_store_access,
    )
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.resolver import CredentialResolver
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.errors.base import AppError
    from application_sdk.errors.leaves import (
        AppPermissionDeniedError,
        AuthError,
        DependencyUnavailableError,
        PreconditionError,
        SourceUnavailableError,
    )
    from application_sdk.execution._temporal.preflight_gate import (
        PreflightSurface,
        emit_preflight_check_outcome,
    )
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
# Check *names* deliberately avoid the "SDR" acronym: the frontend title-cases
# the name by inserting a space before every capital, so "SDR" would render as
# "S D R". The SDR context lives in the (verbatim) messages below instead.
_OBJECT_STORE_CHECK_NAMES: dict[str, str] = {
    "deployment": "Deployment object store",
    "upstream": "Metadata upload connectivity",
}

# User-facing success copy per object-store role. Keeps the interactive
# preflight rows readable; the technical ObjectStoreCheckResult.message is still
# logged worker-side for the operator.
_OBJECT_STORE_SUCCESS_MESSAGES: dict[str, str] = {
    "deployment": (
        "Configured object store is accessible. Read and write access confirmed."
    ),
    "upstream": "SDR worker can upload metadata to Atlan.",
}

# Simple failure copy for the UI — states what failed, not how to fix it (the
# fix is the typed suggested action below). The technical probe error/hint stays
# in the worker log.
_OBJECT_STORE_FAILURE_MESSAGES: dict[str, str] = {
    "deployment": "Configured object store is not accessible.",
    "upstream": "SDR worker cannot upload metadata to Atlan.",
}

# Customer-facing remediation per (role, classifier bucket). The classifier's own
# hint (ObjectStoreCheckResult.hint) is engineer-facing and stays in the log;
# this is the user-facing next step keyed off the same error_class so the precise
# answer is no longer computed and discarded.
_OBJECT_STORE_ACTIONS: dict[tuple[str, str], str] = {
    (
        "deployment",
        "permission denied",
    ): "Grant the object store credentials read and write access to the bucket or container.",
    (
        "deployment",
        "invalid credentials",
    ): "Update the object store credentials — the key may be wrong, expired, or malformed.",
    (
        "deployment",
        "connectivity / unknown",
    ): "Check the object store endpoint, name, and network access from the SDR worker.",
    (
        "upstream",
        "permission denied",
    ): "Grant the upstream credentials read and write access to the Atlan upload target.",
    (
        "upstream",
        "invalid credentials",
    ): "Update the upstream credentials — the key may be wrong, expired, or malformed.",
    (
        "upstream",
        "connectivity / unknown",
    ): "Check that the SDR worker can reach Atlan, and verify the upstream endpoint.",
    (
        "deployment",
        "bucket relocation in progress",
    ): "The object store bucket is being relocated; uploads are rejected until the relocation completes. Retry afterwards — no credential change is needed.",
    (
        "upstream",
        "bucket relocation in progress",
    ): "The Atlan upload target is being relocated; uploads are rejected until the relocation completes. Retry afterwards — no credential change is needed.",
}


def _object_store_failure(
    *,
    label: str,
    error_class: str | None,
    binding_name: str,
    message: str,
    hint: str | None = None,
) -> AppError:
    """Map a failed object-store probe onto the taxonomy leaf for its bucket+role.

    A missing or unparseable binding (``"not configured"``) is a permanent
    deployment config gap, so it maps to ``PreconditionError`` (retryable=False)
    — never a transient outage the platform would retry. Its customer step is the
    probe's own ``hint`` ("add a Dapr component named ..."), which is otherwise
    engineer-facing and discarded.

    Permission and auth buckets use the USER leaves that already match
    (``AppPermissionDeniedError``, ``AuthError``) so a 403 is not retried as a
    transient outage. Remaining availability failures split on role: the
    customer-owned deployment store is ``SourceUnavailableError`` (USER); the
    upstream Atlan upload proxy is ``DependencyUnavailableError`` (PLATFORM).
    Customer-facing ``suggested_action`` is kept in every case.
    """
    bucket = error_class or "connectivity / unknown"
    action = _OBJECT_STORE_ACTIONS.get(
        (label, bucket),
        _OBJECT_STORE_ACTIONS.get((label, "connectivity / unknown")),
    )
    if bucket == "not configured":
        return PreconditionError(
            message=message,
            suggested_action=hint or action,
            resource=binding_name,
            expected_state="configured",
        )
    if bucket == "permission denied":
        return AppPermissionDeniedError(
            message=message,
            suggested_action=action,
            resource=binding_name,
            required_action="read and write",
        )
    if bucket == "invalid credentials":
        return AuthError(
            message=message,
            suggested_action=action,
            failure_reason=bucket,
        )
    if label == "upstream":
        return DependencyUnavailableError(
            message=message,
            suggested_action=action,
            service="object_store",
            target=binding_name,
            network_error=bucket,
        )
    return SourceUnavailableError(
        message=message,
        suggested_action=action,
        source_type=f"{label} object store",
        endpoint=binding_name,
        network_error=bucket,
    )


# Leading row asserting the SDR deployment itself is reachable: if this activity
# is executing, a worker on the customer's task queue picked it up. Name avoids
# the "SDR" acronym (frontend spaces out capitals → "S D R"); the message keeps it.
_SDR_REACHABLE_CHECK_NAME = "Deployment reachability"
_SDR_REACHABLE_MESSAGE = "The SDR worker is reachable."


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
# store can raise them without an SDK release. Defaults are a flat 300s per
# activity (schedule_to_close == start_to_close): a single generous attempt,
# deliberately no in-schedule retry room. Invariant: start_to_close <=
# schedule_to_close in each pair; only a truly *inverted* override
# (start_to_close > schedule_to_close) is warned about at module load (below).
# Computed once at module load; the LM proxy's own WorkflowExecutionTimeout
# backstops sit just above these schedule_to_close values.
#
# NOTE: deliberately NO heartbeat_timeout. These activities run a single
# handler call (test_auth / preflight_check / fetch_metadata) and never call
# activity.heartbeat(), so setting a heartbeat_timeout would hard-cap runtime at
# that interval and fail any real source check that runs longer than it —
# start_to_close is the correct in-flight bound here.
_AUTH_SCHEDULE_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_AUTH_SCHEDULE_TO_CLOSE_SECONDS", 300)
)
_PREFLIGHT_SCHEDULE_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_PREFLIGHT_SCHEDULE_TO_CLOSE_SECONDS", 300)
)
_METADATA_SCHEDULE_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_METADATA_SCHEDULE_TO_CLOSE_SECONDS", 300)
)

_AUTH_START_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_AUTH_START_TO_CLOSE_SECONDS", 300)
)
_PREFLIGHT_START_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_PREFLIGHT_START_TO_CLOSE_SECONDS", 300)
)
_METADATA_START_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_SDR_METADATA_START_TO_CLOSE_SECONDS", 300)
)

# The default pairs are equal (flat 300s cap), but an operator can override
# either half independently. Warn (don't raise) at module load only if a pair is
# truly *inverted* — start_to_close > schedule_to_close — which Temporal rejects
# at schedule time; equal pairs are a valid flat cap and stay silent.
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
    if _start > _schedule:
        logger.warning(
            "SDR %s: start_to_close (%.0fs) > schedule_to_close (%.0fs); Temporal "
            "rejects a start_to_close larger than schedule_to_close. Check the "
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
        # Returns the typed AuthOutput; heracles normalizes it into the frontend
        # envelope at the API boundary (it owns the FE contract).
        return await workflow.execute_activity(
            SDR_TEST_AUTH_ACTIVITY,
            input,
            result_type=AuthOutput,
            retry_policy=_AUTH_RETRY,
            schedule_to_close_timeout=_AUTH_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_AUTH_START_TO_CLOSE,
        )


@workflow.defn(name="sdr:preflight_check")
class SdrPreflightCheckWorkflow:
    """Durable wrapper around ``Handler.preflight_check``."""

    @workflow.run
    async def run(self, input: PreflightInput) -> PreflightOutput:
        # Returns the typed PreflightOutput; heracles normalizes it into the
        # SageV2 envelope at the API boundary (it owns the FE contract).
        return await workflow.execute_activity(
            SDR_PREFLIGHT_ACTIVITY,
            input,
            result_type=PreflightOutput,
            retry_policy=_DEFAULT_RETRY,
            schedule_to_close_timeout=_PREFLIGHT_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_PREFLIGHT_START_TO_CLOSE,
        )


@workflow.defn(name="sdr:fetch_metadata")
class SdrFetchMetadataWorkflow:
    """Durable wrapper around ``Handler.fetch_metadata``."""

    @workflow.run
    async def run(self, input: MetadataInput) -> MetadataOutput:
        # Returns the typed MetadataOutput; heracles unwraps its objects into the
        # filter-tree `results` shape at the API boundary (it owns the FE contract).
        return await workflow.execute_activity(
            SDR_FETCH_METADATA_ACTIVITY,
            input,
            result_type=MetadataOutput,
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
    source/credential checks.  A failed probe carries a typed leaf matched to
    the classifier bucket and store role (permission → ``AppPermissionDeniedError``,
    auth → ``AuthError``, deployment availability → ``SourceUnavailableError``,
    upstream availability → ``DependencyUnavailableError``) so the message,
    remediation, and probe evidence surface in the UI and route to the right
    audience.

    No-op when not in SDR mode (``check_object_store_access`` returns ``[]`` when
    ``ENABLE_ATLAN_UPLOAD`` is falsy).  If any object-store check fails and the
    handler reported ``READY``, the verdict is downgraded to ``NOT_READY``; an
    already ``NOT_READY``/``PARTIAL`` verdict is left untouched. The aggregate
    banner is pinned to the object-store row that triggered the downgrade, not
    the first failed check in ``output.checks`` (a non-fatal secret-store row
    may already be present).

    Never raises — any unexpected error is logged and the handler's own result is
    returned unchanged.
    """
    try:
        start = time.monotonic()
        results = await check_object_store_access(get_infrastructure())
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if not results:
            return

        # SDR mode confirmed (a worker is executing this activity), so the
        # deployment is reachable — surface that as the first check row.
        output.checks.insert(
            0,
            PreflightCheck(
                name=_SDR_REACHABLE_CHECK_NAME,
                passed=True,
                message=_SDR_REACHABLE_MESSAGE,
            ),
        )

        first_failed_object_store: PreflightCheck | None = None
        for result in results:
            name = _OBJECT_STORE_CHECK_NAMES.get(
                result.label, f"{result.label} object store"
            )
            error: AppError | None = None
            if result.passed:
                message = _OBJECT_STORE_SUCCESS_MESSAGES.get(
                    result.label, "Configured object store is accessible."
                )
            else:
                # Simple, non-technical failure copy for the UI; the detailed
                # probe cause + engineer hint go to the worker log.
                message = _OBJECT_STORE_FAILURE_MESSAGES.get(
                    result.label, "Configured object store is not accessible."
                )
                # A failed access probe on a row the customer is about to see is
                # not informational — warn, and actually carry the classifier's
                # engineer-facing hint (its only other reader is the boot path).
                logger.warning(
                    "SDR object-store check failed (%s): %s | hint: %s",
                    result.label,
                    result.message,
                    result.hint or "(none)",
                )
                # Bucket+role → taxonomy leaf. suggested_action stays the
                # customer-facing next step; the engineer hint is logged above.
                # ``hint`` is passed only so the not-configured branch can use the
                # probe's own "add a Dapr component" guidance as the customer step.
                error = _object_store_failure(
                    label=result.label,
                    error_class=result.error_class,
                    binding_name=result.binding_name,
                    message=message,
                    hint=result.hint,
                )
            check = PreflightCheck(
                name=name,
                passed=result.passed,
                message=message,
                error=error,
            )
            if not result.passed and first_failed_object_store is None:
                first_failed_object_store = check
            output.checks.append(check)

        output.total_duration_ms += elapsed_ms
        if (
            first_failed_object_store is not None
            and output.status == PreflightStatus.READY
        ):
            output.status = PreflightStatus.NOT_READY
            # Pin the banner to the object-store row that triggered the
            # downgrade. A non-fatal secret-store failure is inserted before
            # this function runs, so scanning ``output.checks`` for the first
            # failed row would copy that earlier row instead.
            output.error = first_failed_object_store.error
            output.message = first_failed_object_store.resolved_message
    except Exception:
        # Must never break the handler's own preflight result.
        logger.warning(
            "SDR preflight: object-store access check augmentation failed; "
            "leaving handler result unchanged",
            exc_info=True,
        )


def _secret_store_check_row(result: SecretStoreCheckResult) -> PreflightCheck:
    """Build the secret-store preflight check row from a probe result.

    Name avoids the "SDR" acronym (the frontend spaces out capitals). The failure
    is a customer-owned secret store, so both leaf types route to the customer
    (audience=USER): a store outage is a SourceUnavailableError, while a config
    gap (e.g. a multi-key spec with no secret-path, where the store is never
    contacted) is a PreconditionError. Keyed off ``store_down``, not whether a
    fetch happened."""
    error: AppError | None = None
    if not result.passed:
        if result.store_down:
            error = SourceUnavailableError(
                message=result.message,
                suggested_action=result.suggested_action,
                source_type="secret store",
            )
        else:
            error = PreconditionError(
                message=result.message,
                suggested_action=result.suggested_action,
            )
    return PreflightCheck(
        name="Secret store",
        passed=result.passed,
        message=result.message,
        error=error,
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
        secret_row: PreflightCheck | None = None
        if input.agent_json is not None and input.agent_json.is_populated():
            # SDR-only: verify the customer secret store first. A failure is
            # either FATAL (credentials can't resolve — the store is down, or the
            # config can't reach a bundle) or not.
            #
            # A FATAL result short-circuits to NOT_READY with a clear reason:
            # resolving credentials would itself raise (or has nothing to
            # resolve), so there is nothing for the connectivity checks to try.
            #
            # A non-fatal result (reachable store that resolved nothing) is kept
            # as a failed row for visibility, but the run continues: every field
            # falls back to its literal value (see _substitute), so a customer who
            # put raw secrets directly in the workflow config can still connect.
            #
            # On the non-fatal path check_secret_store_access already fetched and
            # substituted the bundle, so reuse ``secret_result.resolved`` instead
            # of resolving again — one store round-trip per preflight, not two.
            infra = get_infrastructure()
            secret_store = infra.secret_store if infra is not None else None
            secret_result = await check_secret_store_access(
                input.agent_json, secret_store
            )
            secret_row = _secret_store_check_row(secret_result)
            if secret_result.fatal:
                output = PreflightOutput(
                    status=PreflightStatus.NOT_READY,
                    message=secret_result.message,
                    error=secret_row.error,
                    checks=[secret_row],
                )
                await _append_object_store_checks(output)
                emit_preflight_check_outcome(
                    logger,
                    binding.app_name,
                    output,
                    surface=PreflightSurface.SDR,
                    entrypoint=input.entrypoint,
                )
                return output
            if secret_result.resolved is not None:
                input.credentials = HandlerCredential.list_from_raw(
                    secret_result.resolved
                )
            else:
                input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            output = await binding.handler.preflight_check(input)
        # SDR-only: fold the secret-store + object-store access checks into the
        # interactive preflight so they show up as UI check rows. Non-raising.
        if secret_row is not None:
            output.checks.insert(0, secret_row)
        await _append_object_store_checks(output)
        emit_preflight_check_outcome(
            logger,
            binding.app_name,
            output,
            surface=PreflightSurface.SDR,
            entrypoint=input.entrypoint,
        )
        return output

    @activity.defn(name=SDR_FETCH_METADATA_ACTIVITY)
    async def fetch_metadata(input: MetadataInput) -> MetadataOutput:
        if input.agent_json is not None and input.agent_json.is_populated():
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            return await binding.handler.fetch_metadata(input)

    return [test_auth, preflight_check, fetch_metadata]
