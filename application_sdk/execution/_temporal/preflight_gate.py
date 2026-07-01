"""Injected pre-extraction preflight gate (HYP-1883).

A CORE SDK extraction-lifecycle activity — deliberately separate from SDR.
Every generated workflow's ``_run`` dispatches ``{app}:preflight`` as its first
step (see :func:`application_sdk.app.base._run_preflight_gate`); when the
verdict's ``should_block`` is set (a failed blocking check) the run aborts
before extraction.

This is NOT an SDR feature: SDR exposes the handler operations as durable
Temporal *workflows* for remote control-plane calls, whereas the gate is an
in-workflow guard that runs in every extraction worker. The only thing reused
from :mod:`application_sdk.execution._temporal.sdr` is the handler-context
binder (``_bind_context``), so the gate invokes ``handler.preflight_check`` the
exact same way the HTTP and SDR paths do. Credential resolution happens *inside*
the activity (mirrors ``templates/sql_app.py`` ``_init_sql_client``) — the
deterministic workflow only forwards secret-free references.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.resolver import CredentialResolver
    from application_sdk.execution._temporal.sdr import _bind_context, _SdrBinding
    from application_sdk.handler.contracts import (
        HandlerCredential,
        PreflightGateInput,
        PreflightInput,
        PreflightOutput,
    )
    from application_sdk.infrastructure.context import get_infrastructure
    from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from application_sdk.handler.base import Handler


def preflight_gate_activity_name(app_name: str) -> str:
    """Activity name for the gate: ``{app}:preflight``.

    App-namespaced (like the app's own ``{app}:<task>`` activities) so the gate
    reads as a native step of the workflow in the Temporal UI, rather than a
    foreign ``sdr:``/``preflight:`` activity. The workflow's ``_run`` and the
    worker registration must derive the name from the same ``app_name``.
    """
    return f"{app_name}:preflight"


# Dispatched from the extraction workflow's _run. Bounded so a slow/unreachable
# source can't stall extraction start indefinitely. start_to_close is sized so
# two attempts (plus backoff) fit inside schedule_to_close — otherwise the retry
# is cosmetic (the second attempt can't run before the schedule cap fires).
_GATE_SCHEDULE_TO_CLOSE = timedelta(seconds=60)
_GATE_START_TO_CLOSE = timedelta(seconds=25)
_GATE_RETRY = RetryPolicy(maximum_attempts=2, backoff_coefficient=2)


def build_preflight_gate_activity(
    handler: Handler,
    app_name: str,
) -> Callable[..., Awaitable[Any]]:
    """Build the injected preflight-gate activity (``{app}:preflight``).

    Registered unconditionally by the worker (independent of the SDR opt-out)
    because the gate is mandatory. Reuses SDR's ``_bind_context`` so the handler
    runs with the same per-invocation context the HTTP/SDR paths provide.
    """
    binding = _SdrBinding(handler=handler, app_name=app_name)

    @activity.defn(name=preflight_gate_activity_name(app_name))
    async def preflight_gate(input: PreflightGateInput) -> PreflightOutput:
        # Resolve inside the activity (the workflow forwarded only references).
        ref = CredentialRef.resolve_or_none(input, prebuilt=input.credential_ref)
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
        # Verdict record for debuggability: the gate decision (should_block),
        # the advisory status, and the check count.
        logger.info(
            "Preflight gate verdict: %s (entrypoint=%s, status=%s, checks=%d)",
            "block" if result.should_block else "proceed",
            input.entrypoint or "<implicit>",
            result.status.value,
            len(result.checks),
        )
        return result

    return preflight_gate
