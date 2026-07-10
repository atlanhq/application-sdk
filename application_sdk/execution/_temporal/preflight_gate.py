"""Injected pre-extraction preflight gate (HYP-1883).

A core SDK extraction-lifecycle activity. Every generated workflow's ``_run``
dispatches ``{app}:preflight`` as its first step (see
:func:`application_sdk.app.base._run_preflight_gate`). The activity raises
``ApplicationError(type="PreflightFailed")`` when the verdict is ``NOT_READY``
so the abort shows red in Temporal and attributes to preflight; ``READY`` and
``PARTIAL`` return normally. Credential resolution happens *inside* the activity
— the deterministic workflow only forwards the secret-free
:class:`PreflightGateInput`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from application_sdk.credentials.ref import CredentialRef, CredentialResolvable
    from application_sdk.credentials.resolver import CredentialResolver
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.errors.leaves import (
        DependencyUnavailableError,
        PreconditionError,
    )
    from application_sdk.handler.context import bind_invocation_context
    from application_sdk.handler.contracts import (
        BaseConnectionConfig,
        BaseMetadataConfig,
        HandlerCredential,
        PreflightCheck,
        PreflightInput,
        PreflightOutput,
        PreflightStatus,
    )
    from application_sdk.infrastructure.context import get_infrastructure
    from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

PREFLIGHT_FAILED_ERROR_TYPE = "PreflightFailed"


def is_preflight_block(exc: BaseException | None) -> bool:
    """Whether ``exc`` (or any cause in its chain) is the deliberate gate block.

    The activity raises ``ApplicationError(type="PreflightFailed")``; Temporal
    wraps it in an ``ActivityError``, so the marker may sit on a cause rather
    than the top-level error.
    """
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE:
            return True
        nxt = getattr(current, "cause", None)
        current = nxt if nxt is not None else current.__cause__
    return False


def input_type_supports_gate(input_type: type) -> bool:
    """Whether an entrypoint's input type is gate-eligible.

    Lifts the runtime ``isinstance(input, CredentialResolvable)`` guard (see
    ``_run_preflight_gate``) to the *type* level so the worker can warn once at
    boot instead of skipping silently on every run. Checks that the three
    protocol members are declared as top-level Pydantic ``model_fields`` —
    declaring them (rather than carrying them as Pydantic extras) is the
    portable way to satisfy the guard across supported Python versions.
    """
    fields = getattr(input_type, "model_fields", None)
    if not fields:
        return False
    return all(name in fields for name in CredentialResolvable.__annotations__)


if TYPE_CHECKING:
    from application_sdk.handler.base import Handler


class PreflightGateInput(BaseModel):
    """Secret-free routing envelope threaded from the extraction input into the
    gate activity.

    Built deterministically inside the generated workflow ``_run`` from the
    extraction ``input_data`` (which satisfies
    :class:`~application_sdk.credentials.ref.CredentialResolvable`). Carries only
    references — resolution happens inside the gate activity, never in the
    deterministic workflow. Lives beside the gate (not in ``handler/contracts``)
    because handlers never see it — it is the workflow-to-activity envelope.
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

    extraction_snapshot: dict[str, Any] = Field(default_factory=dict)
    """Raw ``model_dump(mode='json')`` of the extraction input.

    Stored here (secret-free routing fields only carry refs, not creds) so the
    gate activity can build ``PreflightInput.metadata`` inside the activity frame
    rather than in the deterministic workflow — preventing app-authored field reads
    (e.g. filter config) from running in a non-deterministic context on replay.
    """

    @classmethod
    def from_extraction_input(
        cls, input_data: Any, entrypoint: str
    ) -> PreflightGateInput:
        """Build the gate input from a workflow extraction input — never raises.

        Collects only secret-free credential routing fields plus the raw
        ``model_dump`` snapshot so the gate activity can derive form config in the
        activity frame. If a field does not fit (an oddly-shaped custom input),
        degrades to a minimal input — the gate must fail open *before* dispatch.
        """
        snapshot: dict[str, Any] = {}
        if hasattr(input_data, "model_dump"):
            try:
                snapshot = input_data.model_dump(mode="json")
            except Exception:  # never raise — the gate must fail open before dispatch
                logger.debug(
                    "Could not snapshot extraction input; gate proceeds without form config",
                    exc_info=True,
                )
        base_kw: dict[str, Any] = dict(
            extraction_method=getattr(input_data, "extraction_method", "") or "",
            credential_guid=getattr(input_data, "credential_guid", "") or "",
            agent_json=getattr(input_data, "agent_json", None),
            credential_ref=getattr(input_data, "credential_ref", None),
            entrypoint=entrypoint,
            extraction_snapshot=snapshot,
        )
        try:
            return cls(**base_kw)
        except ValidationError:
            logger.warning(
                "Extraction input did not fit PreflightGateInput; using a minimal "
                "gate input so the gate still runs",
                exc_info=True,
            )
            return cls(entrypoint=entrypoint)


def preflight_gate_activity_name(app_name: str) -> str:
    """Activity name for the gate: ``{app}:preflight``.

    App-namespaced like the app's own ``{app}:<task>`` activities. The workflow's
    ``_run`` and the worker registration must derive the name from the same
    ``app_name``.
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


_ROUTING_KEYS: frozenset[str] = frozenset(
    {"extraction_method", "credential_guid", "agent_json", "credential_ref"}
)


_EMPTY_CONFIG_VALUES: tuple[Any, ...] = (None, "", (), [], {})


def _config_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Extract preflight form config from a raw extraction-input snapshot.

    Called inside the gate activity (activity frame, not workflow) so any
    field reads by the app never run in a deterministic context on replay.
    Produces both the original field name and its hyphenated equivalent so
    handlers that use either naming convention work on the gate path.

    Drops credential-routing fields and *genuinely* empty values (None, empty
    string, empty container) — but preserves ``False`` and ``0`` so a handler
    reading a bool/int config field off ``PreflightInput.metadata`` sees the
    real value, not a silent default.
    """
    config: dict[str, Any] = {}
    for k, v in snapshot.items():
        if k in _ROUTING_KEYS or v in _EMPTY_CONFIG_VALUES:
            continue
        config[k] = v
        if "_" in k:
            config[k.replace("_", "-")] = v
    return config


def _dump_check(check: PreflightCheck) -> dict[str, Any]:
    dumped = check.model_dump(mode="json", exclude_none=True)
    dumped["message"] = check.resolved_message
    return dumped


def _build_block_error(result: PreflightOutput, app_name: str) -> Any:
    """Build the ``PreflightFailed`` ApplicationError for a NOT_READY verdict.

    ``details[0]`` is the primary failure's typed ``FailureDetails`` (first failed
    check with an ``error``; else the first failed check's message wrapped in
    ``PreconditionError``). ``details[1]`` carries every check so the red activity
    pane shows them — a failed activity has no result payload.
    """
    from application_sdk.execution.errors import ApplicationError  # noqa: PLC0415

    failed = [c for c in result.checks if not c.passed]
    primary = next((c for c in failed if c.error is not None), None)
    if primary is not None and primary.error is not None:
        details = primary.error
        if details.app_name is None:
            details = details.model_copy(update={"app_name": app_name})
    else:
        fallback = failed[0].resolved_message if failed else ""
        details = PreconditionError(
            message=fallback or "Preflight check failed",
            app_name=app_name,
            retryable=False,
        ).to_failure_details()

    joined = "; ".join(m for m in (c.resolved_message for c in failed) if m)
    reason = (
        result.message or joined or "Preflight check failed; aborting before extraction"
    )
    checks_payload = {"checks": [_dump_check(c) for c in result.checks]}
    return ApplicationError(
        f"Preflight failed: {reason}",
        details,
        checks_payload,
        type=PREFLIGHT_FAILED_ERROR_TYPE,
        non_retryable=True,
    )


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
        # Build form config from the extraction-input snapshot in the activity
        # frame so app field reads stay outside the deterministic workflow.
        metadata_dump = _config_from_snapshot(input.extraction_snapshot)
        preflight_input = PreflightInput(
            credentials=credentials,
            entrypoint=input.entrypoint,
            metadata=BaseMetadataConfig(**metadata_dump),
            connection_config=BaseConnectionConfig(**metadata_dump),
        )
        with bind_invocation_context(app_name, credentials):
            result = await handler.preflight_check(preflight_input)
        blocked = result.status is PreflightStatus.NOT_READY
        logger.info(
            "Preflight gate verdict: %s (entrypoint=%s, status=%s, checks=%d)",
            "block" if blocked else "proceed",
            input.entrypoint or "<implicit>",
            result.status.value,
            len(result.checks),
        )
        if blocked:
            raise _build_block_error(result, app_name)
        return result

    return preflight_gate
