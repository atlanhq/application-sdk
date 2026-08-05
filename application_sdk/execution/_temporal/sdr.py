"""Handler operations as durable Temporal workflows.

Exposes ``test_auth``, ``preflight_check`` and ``fetch_metadata`` as generic,
per-app, retryable workflows, alongside the HTTP endpoints in
:mod:`application_sdk.handler.service`. At worker assembly time,
:func:`build_sdr_activities` binds the concrete Handler into activity closures;
the workflows reference them by name.

Originally built for SDR (Self Deployed Runtime), where the app runs on customer
infrastructure and Atlan's control plane cannot reach it over HTTP — hence the
``sdr:`` names, which stay registered because LM starts them by name. Nothing about
them is actually SDR-specific, though: a durable, per-app, retryable wrapper around a
handler operation is exactly what an interactive test needs on *any* deployment, and
what proactive drift detection needs. So the same wrappers are also registered under
``checks:*``, which is what new callers should use, and a fourth workflow —
``checks:scheduled_preflight`` — serves the Automation Engine's periodic probe.

The verdict itself comes from :mod:`application_sdk.checks`, shared with the config
UI and the pre-run gate. What differs per workflow here is policy: whether a source
we could not verify fails the activity (interactive: yes, a human is waiting;
scheduled: no, a retry-and-alert on a slow source is worse than an honest verdict),
and how patient the timeouts are.

Registered by default and silently skipped when the worker is started without a
Handler (see ``create_worker(handler=...)``).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from application_sdk.checks.cadence import recheck_after_seconds
    from application_sdk.checks.outcome import emit as emit_outcome
    from application_sdk.checks.outcome import outcome_for
    from application_sdk.checks.request import CheckRequest, CheckTrigger
    from application_sdk.checks.runner import run_checks
    from application_sdk.checks.verdict import CheckClassification
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.resolver import CredentialResolver
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.errors.leaves import DependencyUnavailableError
    from application_sdk.handler.base import implements_test_auth
    from application_sdk.handler.context import bind_invocation_context
    from application_sdk.handler.contracts import (
        AuthInput,
        AuthOutput,
        AuthStatus,
        HandlerCredential,
        MetadataInput,
        MetadataOutput,
        PreflightInput,
        PreflightOutput,
    )
    from application_sdk.infrastructure.context import get_infrastructure
    from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.handler.base import Handler

logger = get_logger(__name__)

SDR_TEST_AUTH_ACTIVITY = "sdr:test_auth"
SDR_PREFLIGHT_ACTIVITY = "sdr:preflight_check"
SDR_FETCH_METADATA_ACTIVITY = "sdr:fetch_metadata"

# Activity for the scheduled probe. A separate activity, not a flag, because it
# stamps a different trigger and returns a cadence recommendation the interactive
# paths have no use for.
CHECKS_SCHEDULED_ACTIVITY = "checks:scheduled_preflight"

# Workflow names. ``sdr:*`` (above, on the workflow classes) stays registered because
# LM starts those by name; ``checks:*`` is what new callers should use, since none of
# this is SDR-specific — the same durable, per-app wrapper serves an interactive test
# on any deployment and the Automation Engine's proactive drift detection.
CHECKS_TEST_AUTH_WORKFLOW = "checks:test_auth"
CHECKS_PREFLIGHT_WORKFLOW = "checks:preflight_check"
CHECKS_FETCH_METADATA_WORKFLOW = "checks:fetch_metadata"
CHECKS_SCHEDULED_WORKFLOW = "checks:scheduled_preflight"

# Error type for an interactive preflight that could not reach a verdict. Distinct
# from the gate's ``PreflightFailed``, which the log interceptor treats as a
# deliberate pre-run abort — an interactive test that could not reach the source is
# a failure to report, not a policy decision.
SDR_UNVERIFIABLE_ERROR_TYPE = "PreflightUnverifiable"


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

# The scheduled probe can afford to be patient: nobody is watching a spinner, and a
# verdict half an hour late is far better than a wrong verdict now. It gets a longer
# wall clock and one more attempt than the interactive paths, so a source that is
# merely slow (a cold warehouse resuming) reports READY rather than being recorded as
# drift and paging someone.
_SCHEDULED_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=2)
_SCHEDULED_START_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_CHECKS_SCHEDULED_START_TO_CLOSE_SECONDS", 300)
)
_SCHEDULED_SCHEDULE_TO_CLOSE = timedelta(
    seconds=_env_seconds("ATLAN_CHECKS_SCHEDULED_SCHEDULE_TO_CLOSE_SECONDS", 1200)
)


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


@workflow.defn(name=CHECKS_TEST_AUTH_WORKFLOW)
class CheckAuthWorkflow:
    """``checks:test_auth`` — the same operation under its non-SDR name.

    These wrappers were only ever "SDR" by deployment accident: generic, per-app and
    durable is exactly what an interactive test needs on any deployment, and what a
    scheduled probe needs. The ``sdr:*`` names stay registered because LM starts them
    by name; new callers should use these.

    Spelled out rather than subclassing the ``sdr:*`` class: Temporal requires
    ``@workflow.run`` on the class itself and rejects an inherited one, so the
    delegation has to be explicit. Both names dispatch the same activity, so there is
    one implementation regardless.
    """

    @workflow.run
    async def run(self, input: AuthInput) -> AuthOutput:
        return await workflow.execute_activity(
            SDR_TEST_AUTH_ACTIVITY,
            input,
            retry_policy=_AUTH_RETRY,
            schedule_to_close_timeout=_AUTH_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_AUTH_START_TO_CLOSE,
        )


@workflow.defn(name=CHECKS_PREFLIGHT_WORKFLOW)
class CheckPreflightWorkflow:
    """``checks:preflight_check`` — see :class:`CheckAuthWorkflow`."""

    @workflow.run
    async def run(self, input: PreflightInput) -> PreflightOutput:
        return await workflow.execute_activity(
            SDR_PREFLIGHT_ACTIVITY,
            input,
            retry_policy=_DEFAULT_RETRY,
            schedule_to_close_timeout=_PREFLIGHT_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_PREFLIGHT_START_TO_CLOSE,
        )


@workflow.defn(name=CHECKS_FETCH_METADATA_WORKFLOW)
class CheckFetchMetadataWorkflow:
    """``checks:fetch_metadata`` — see :class:`CheckAuthWorkflow`."""

    @workflow.run
    async def run(self, input: MetadataInput) -> MetadataOutput:
        return await workflow.execute_activity(
            SDR_FETCH_METADATA_ACTIVITY,
            input,
            retry_policy=_DEFAULT_RETRY,
            schedule_to_close_timeout=_METADATA_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_METADATA_START_TO_CLOSE,
        )


@workflow.defn(name=CHECKS_SCHEDULED_WORKFLOW)
class ScheduledCheckWorkflow:
    """``checks:scheduled_preflight`` — proactive drift detection for one connection.

    A separate workflow rather than a flag on the interactive one, so the *caller*
    expresses intent by choosing what to start. Two things follow from that which a
    flag could not give: the outcome row's ``trigger`` cannot be mis-stamped by a
    caller that forgot to set it, and the retry/timeout profile can differ — nobody is
    waiting on this answer, so it can afford to be patient where the UI cannot.

    Returns the verdict with ``recheck_after_seconds`` populated, which is the whole
    interface for adaptive cadence: the scheduler honours that number and needs to
    know nothing else about what was checked.
    """

    @workflow.run
    async def run(self, input: PreflightInput) -> PreflightOutput:
        return await workflow.execute_activity(
            CHECKS_SCHEDULED_ACTIVITY,
            input,
            retry_policy=_SCHEDULED_RETRY,
            schedule_to_close_timeout=_SCHEDULED_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=_SCHEDULED_START_TO_CLOSE,
        )


SDR_WORKFLOWS: tuple[type, ...] = (
    SdrTestAuthWorkflow,
    SdrPreflightCheckWorkflow,
    SdrFetchMetadataWorkflow,
    CheckAuthWorkflow,
    CheckPreflightWorkflow,
    CheckFetchMetadataWorkflow,
    ScheduledCheckWorkflow,
)
"""Every handler-op workflow the worker registers.

Named ``SDR_WORKFLOWS`` for continuity with the worker that imports it, though the
set is no longer SDR-specific: the ``checks:*`` names serve interactive tests on any
deployment and the scheduled probe, and the ``sdr:*`` names remain because LM starts
them by name.
"""


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
        """Interactive auth test.

        Handlers that still implement ``test_auth`` themselves keep the path they
        have always taken — their behaviour is what that app's users see today.
        Handlers on the inherited default go through the shared core as an
        ``AUTH``-depth run, which is the same handler method (and the same resolved
        credential) the run itself will use.
        """
        if not implements_test_auth(binding.handler):
            request = CheckRequest.from_preflight_input(
                input.to_preflight_input(),
                app_name=binding.app_name,
                trigger=CheckTrigger.UI_AUTH,
                budget_seconds=_AUTH_START_TO_CLOSE.total_seconds(),
            )
            verdict = await run_checks(
                binding.handler,
                request,
                # An object store has no bearing on whether a credential works, and
                # auth is meant to be the cheapest question the UI can ask.
                augment_object_store=False,
            )
            emit_outcome(
                verdict,
                outcome=outcome_for(blocked=not verdict.is_ready, enforce=False),
                reason=verdict.status.value,
            )
            if verdict.classification is CheckClassification.SOURCE_UNVERIFIABLE:
                # Never got far enough to judge the credential — say so, rather than
                # telling the user their password is wrong when the host was down.
                return AuthOutput(
                    status=AuthStatus.FAILED, message=verdict.output.message
                )
            return AuthOutput.from_preflight_output(verdict.output)

        if input.agent_json is not None and input.agent_json.is_populated():
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            return await binding.handler.test_auth(input)

    @activity.defn(name=SDR_PREFLIGHT_ACTIVITY)
    async def preflight_check(input: PreflightInput) -> PreflightOutput:
        """Interactive preflight, on the shared check core.

        Everything that decides the answer — credential resolution, the budget net
        of it, the object-store probe, the classification, the outcome row — is the
        same code the pre-run gate uses. What stays local is one policy choice: a
        source we could not verify still **fails the activity** here, because a
        human is waiting on a "Test connection" button and LM's proxy turns a failed
        activity into the error the UI shows. The gate makes the opposite choice
        (report and let posture decide) because nobody is watching.
        """
        request = CheckRequest.from_preflight_input(
            input,
            app_name=binding.app_name,
            trigger=CheckTrigger.SDR,
            budget_seconds=_PREFLIGHT_START_TO_CLOSE.total_seconds(),
        )
        verdict = await run_checks(binding.handler, request)
        emit_outcome(
            verdict,
            outcome=outcome_for(blocked=not verdict.is_ready, enforce=False),
            reason=(
                verdict.output.checks[0].error.code
                if verdict.classification is CheckClassification.SOURCE_UNVERIFIABLE
                and verdict.output.checks
                and verdict.output.checks[0].error
                else verdict.status.value
            ),
        )
        if verdict.classification is CheckClassification.SOURCE_UNVERIFIABLE:
            # Fail the activity, as this path always has — but with a typed,
            # attributed, redacted failure instead of whatever the driver raised.
            # Deliberately not the gate's block type: the log interceptor reads that
            # as a *deliberate* pre-run abort, which this is not.
            from application_sdk.execution.errors import (  # noqa: PLC0415 — avoid import cycle at module load
                ApplicationError,
            )

            details = verdict.output.checks[0].error if verdict.output.checks else None
            raise ApplicationError(
                verdict.output.message or "Preflight could not verify the source",
                *([details] if details is not None else []),
                type=SDR_UNVERIFIABLE_ERROR_TYPE,
                non_retryable=True,
            )
        return verdict.output

    @activity.defn(name=CHECKS_SCHEDULED_ACTIVITY)
    async def scheduled_preflight(input: PreflightInput) -> PreflightOutput:
        """Proactive drift detection for one connection.

        The point of this path is to find out that access broke *before* the next
        real run does. So it differs from the interactive one in exactly two ways,
        both because nobody is waiting on the answer:

        * It **never raises** on a source it could not verify. A failed activity would
          make the scheduler retry-and-alert on what is often just a slow source, and
          the honest ``NOT_READY`` verdict is the more useful record. Only our own
          plumbing failures propagate (from the core), where a retry is the right
          response.
        * It returns ``recheck_after_seconds``, so the scheduler's next fire is paced
          by what was actually found rather than a fixed interval — a broken
          connection is looked at again soon, an all-green one is left alone.

        Advisory by design: this blocks nothing and pauses nothing. Enforcement stays
        with the pre-run gate, whose posture ladder already exists for it.
        """
        request = CheckRequest.from_preflight_input(
            input,
            app_name=binding.app_name,
            trigger=CheckTrigger.SCHEDULED,
            budget_seconds=_SCHEDULED_START_TO_CLOSE.total_seconds(),
        )
        verdict = await run_checks(binding.handler, request)
        emit_outcome(
            verdict,
            outcome=outcome_for(blocked=not verdict.is_ready, enforce=False),
            reason=verdict.status.value,
        )
        output = verdict.output
        output.recheck_after_seconds = recheck_after_seconds(verdict)
        return output

    @activity.defn(name=SDR_FETCH_METADATA_ACTIVITY)
    async def fetch_metadata(input: MetadataInput) -> MetadataOutput:
        if input.agent_json is not None and input.agent_json.is_populated():
            input.credentials = await _resolve_agent_credentials(input.agent_json)
        with bind_invocation_context(binding.app_name, input.credentials):
            return await binding.handler.fetch_metadata(input)

    return [test_auth, preflight_check, scheduled_preflight, fetch_metadata]
