"""Injected pre-extraction preflight gate (HYP-1883).

A core SDK extraction-lifecycle activity. Every generated workflow's ``_run``
dispatches ``{app}:preflight`` as its first step (see
:func:`application_sdk.app.base._run_preflight_gate`); when the verdict's
``should_block`` is set (a failed blocking check) the run aborts before
extraction. Credential resolution happens *inside* the activity (mirrors
``templates/sql_app.py`` ``_init_sql_client``) — the deterministic workflow only
forwards the secret-free :class:`PreflightGateInput`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.resolver import CredentialResolver
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.errors.leaves import DependencyUnavailableError
    from application_sdk.handler.context import bind_invocation_context
    from application_sdk.handler.contracts import (
        BaseConnectionConfig,
        BaseMetadataConfig,
        HandlerCredential,
        PreflightInput,
        PreflightOutput,
        PreflightStatus,
    )
    from application_sdk.infrastructure.context import get_infrastructure
    from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from application_sdk.handler.base import Handler


class PreflightGateInput(BaseModel):
    """Secret-free routing envelope the injected gate threads from the extraction
    input into the gate activity.

    Built deterministically inside the generated workflow ``_run`` from the
    extraction ``input_data`` (which satisfies
    :class:`~application_sdk.credentials.ref.CredentialResolvable`). Carries only
    references — resolution happens inside the gate activity, never in the
    deterministic workflow. Declares the
    ``extraction_method``/``credential_guid``/``agent_json`` triple so it
    satisfies ``CredentialResolvable`` and ``CredentialRef.resolve`` works on it
    directly.

    Lives beside the gate (not in ``handler/contracts``) because handlers never
    see it — it is the workflow-to-activity envelope, not a handler contract.

    No ``connection_config`` is threaded: the extraction input carries none
    (``connection_config`` is a UI-form field on the HTTP ``/check`` path only).
    On the gate path a handler reads connectivity from the resolved
    ``credentials`` the activity injects into :class:`PreflightInput`.
    """

    extraction_method: str = ""
    """Credential routing mode (e.g. ``agent`` / ``direct``)."""

    credential_guid: str = ""
    """Platform credential GUID for direct (vault) resolution."""

    agent_json: AgentCredentialSpec | None = None
    """Agent-shape credential spec for inline (secret-manager) resolution."""

    credential_ref: CredentialRef | None = None
    """Pre-built reference, when the extraction input already carries one."""

    entrypoint: str = ""
    """Bare entry-point name of the gated workflow (for per-entrypoint checks)."""

    metadata: BaseMetadataConfig = Field(default_factory=BaseMetadataConfig)
    """Form-level metadata forwarded to the handler, mirroring the HTTP path.
    Empty on the standard extraction path (the SDK input carries no metadata)."""

    @classmethod
    def from_extraction_input(
        cls, input_data: Any, entrypoint: str
    ) -> PreflightGateInput:
        """Build the gate input from a workflow extraction input — never raises.

        Reads only secret-free routing refs plus ``entrypoint``/``metadata`` via
        getattr, so it works for any input satisfying ``CredentialResolvable``.
        If a field does not fit (an oddly-shaped custom input), it degrades to a
        minimal input rather than raising — the gate must fail open *before* the
        dispatch, not only during it.
        """
        # Form config for the handler's filter/scope checks. Extraction inputs
        # flatten the AE payload's nested ``metadata`` into typed top-level
        # fields, so a literal ``.metadata`` is usually absent — prefer an
        # explicit ``preflight_config()`` hook that reconstructs the dict the
        # handler reads, and fall back to a literal ``metadata`` attr otherwise.
        # Without this the handler runs on the gate path with empty config and
        # its filter/scope checks silently no-op.
        config: Any = None
        hook = getattr(input_data, "preflight_config", None)
        if callable(hook):
            try:
                config = hook()
            except Exception:  # never raise — the gate must fail open before dispatch
                logger.warning(
                    "preflight_config() raised; gate proceeds without form config",
                    exc_info=True,
                )
                config = None
        if config is None:
            metadata = getattr(input_data, "metadata", None)
            config = (
                metadata if isinstance(metadata, (dict, BaseMetadataConfig)) else None
            )
        metadata_kw = {"metadata": config} if config is not None else {}
        base_kw: dict[str, Any] = dict(
            extraction_method=getattr(input_data, "extraction_method", "") or "",
            credential_guid=getattr(input_data, "credential_guid", "") or "",
            agent_json=getattr(input_data, "agent_json", None),
            credential_ref=getattr(input_data, "credential_ref", None),
            entrypoint=entrypoint,
        )
        try:
            return cls(**base_kw, **metadata_kw)
        except ValidationError:
            logger.warning(
                "Extraction input did not fit PreflightGateInput; using a minimal "
                "gate input so the gate still runs",
                exc_info=True,
            )
            return cls(entrypoint=entrypoint)


def preflight_gate_activity_name(app_name: str) -> str:
    """Activity name for the gate: ``{app}:preflight``.

    App-namespaced (like the app's own ``{app}:<task>`` activities) so the gate
    reads as a native step of the workflow in the Temporal UI, rather than a
    foreign ``sdr:``/``preflight:`` activity. The workflow's ``_run`` and the
    worker registration must derive the name from the same ``app_name``.
    """
    from application_sdk.app.registry import (  # noqa: PLC0415 — avoid import cycle at module load
        get_activity_name,
    )

    return get_activity_name(app_name, "preflight")


# Dispatched from the extraction workflow's _run. Bounded so a slow/unreachable
# source can't stall extraction start indefinitely. start_to_close is sized so
# two attempts (plus backoff) fit inside schedule_to_close — otherwise the retry
# is cosmetic (the second attempt can't run before the schedule cap fires).
GATE_SCHEDULE_TO_CLOSE = timedelta(seconds=60)
GATE_START_TO_CLOSE = timedelta(seconds=25)
GATE_RETRY = RetryPolicy(maximum_attempts=2, backoff_coefficient=2)


def build_preflight_gate_activity(
    handler: Handler,
    app_name: str,
) -> Callable[..., Awaitable[Any]]:
    """Build the injected preflight-gate activity (``{app}:preflight``).

    Registered unconditionally by the worker (independent of the SDR opt-out)
    because the gate is mandatory. Binds the same per-invocation handler context
    the HTTP and SDR paths use (:func:`bind_invocation_context`).
    """

    @activity.defn(name=preflight_gate_activity_name(app_name))
    async def preflight_gate(input: PreflightGateInput) -> PreflightOutput:
        # Resolve inside the activity (the workflow forwarded only references).
        ref = CredentialRef.resolve_or_none(input)
        credentials: list[HandlerCredential] = []
        if ref is not None:
            infra = get_infrastructure()
            secret_store = infra.secret_store if infra is not None else None
            if secret_store is None:
                # A ref exists but there is no store to dereference it — an infra
                # failure, not a valid empty-credential state. Raise so the
                # workflow's fail-open path handles it, rather than calling the
                # handler with empty creds and misattributing the block as AUTH.
                raise DependencyUnavailableError(
                    message="No secret store available to resolve preflight credentials",
                    service="secret_store",
                )
            raw = await CredentialResolver(secret_store).resolve_raw(ref) or {}
            credentials = HandlerCredential.list_from_raw(raw)
        # Mirror the form config into both ``metadata`` and ``connection_config``
        # so a handler reading either field sees it — matching the HTTP /check
        # path's ``_normalize_preflight_request`` behaviour.
        metadata_dump = input.metadata.model_dump() if input.metadata else {}
        preflight_input = PreflightInput(
            credentials=credentials,
            entrypoint=input.entrypoint,
            metadata=input.metadata,
            connection_config=BaseConnectionConfig(**metadata_dump),
        )
        with bind_invocation_context(app_name, credentials):
            result = await handler.preflight_check(preflight_input)
        # Verdict record for debuggability: the gate decision (should_block),
        # the advisory status, and the check count.
        logger.info(
            "Preflight gate verdict: %s (entrypoint=%s, status=%s, checks=%d)",
            "block" if result.should_block else "proceed",
            input.entrypoint or "<implicit>",
            result.status.value,
            len(result.checks),
        )
        # Catch the un-migrated-handler trap: a handler that reports a problem via
        # advisory status but marks no check blocking lets the run proceed — the
        # backward-compat default, but almost always a forgotten blocking=True.
        if not result.should_block and result.status in (
            PreflightStatus.NOT_READY,
            PreflightStatus.PARTIAL,
        ):
            logger.warning(
                "Preflight reported %s but no check is marked blocking — the gate will "
                "NOT stop this run. If a failing check must abort extraction, set "
                "blocking=True on it in Handler.preflight_check (entrypoint=%s, checks=%d).",
                result.status.value,
                input.entrypoint or "<implicit>",
                len(result.checks),
            )
        return result

    return preflight_gate
