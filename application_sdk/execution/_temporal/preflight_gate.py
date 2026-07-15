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

from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from application_sdk.credentials.errors import CredentialNotFoundError
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

# Stable log body for the gate outcome event — the contract connector-pulse queries
# on. Pinned here so the string can't drift across the emission sites.
PREFLIGHT_OUTCOME_EVENT = "Preflight gate outcome"

# Contract sentinel stamped as the primary FailureDetails.code on a fallback block
# (a handler that returned NOT_READY without a typed check error). It replaces the
# generic PRECONDITION code so the outcome event's ``reason`` distinguishes an
# un-migrated block from a typed one (whose reason is the handler error's own code,
# e.g. AUTH). category/audience/retryable are unchanged.
PREFLIGHT_FALLBACK_CODE = "PREFLIGHT_CHECK_FAILED"


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

    Multi-credential apps qualify through this same triple: ``preflight_credential_refs``
    is an additive opt-in resolved separately by the gate, not a replacement — an
    input declaring only the named refs and none of the triple is (correctly)
    gate-ineligible.
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

    credential_ref_fields: dict[str, str] = Field(default_factory=dict)
    """Named credential refs to resolve on the gate path, ``{ref_name: guid_field}``
    — copied secret-free from the input's
    ``ExtractionInput.preflight_credential_refs`` (which documents the shape and
    when it applies). Resolved from :attr:`extraction_snapshot` in the activity."""

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
        input_type = type(input_data)
        credential_ref_fields = dict(
            getattr(input_type, "preflight_credential_refs", {}) or {}
        )
        if not credential_ref_fields and "preflight_credential_refs" in getattr(
            input_type, "model_fields", {}
        ):
            # Declared as a pydantic field, not a ClassVar: the class-level read
            # above returns {}, so a real multi-credential app would silently fall
            # back to single-credential resolution and block healthy runs.
            logger.warning(
                "preflight_credential_refs is declared as a model field, not a "
                "ClassVar; the gate cannot read it and falls back to "
                "single-credential resolution — declare it as ClassVar[dict[str, str]]",
                input_type=input_type.__name__,
            )
        base_kw: dict[str, Any] = dict(
            extraction_method=getattr(input_data, "extraction_method", "") or "",
            credential_guid=getattr(input_data, "credential_guid", "") or "",
            agent_json=getattr(input_data, "agent_json", None),
            credential_ref=getattr(input_data, "credential_ref", None),
            entrypoint=entrypoint,
            credential_ref_fields=credential_ref_fields,
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


def _config_from_snapshot(
    snapshot: dict[str, Any], drop_keys: Iterable[str] = ()
) -> dict[str, Any]:
    """Extract preflight form config from a raw extraction-input snapshot.

    Called inside the gate activity (activity frame, not workflow) so any
    field reads by the app never run in a deterministic context on replay.
    Produces both the original field name and its hyphenated equivalent so
    handlers that use either naming convention work on the gate path.

    Drops credential-routing fields, any ``drop_keys`` (the named-credential
    guid fields, which are refs not form config), and *genuinely* empty values
    (None, empty string, empty container) — but preserves ``False`` and ``0`` so
    a handler reading a bool/int config field off ``PreflightInput.metadata``
    sees the real value, not a silent default.
    """
    dropped = _ROUTING_KEYS | set(drop_keys)
    config: dict[str, Any] = {}
    for k, v in snapshot.items():
        if k in dropped or v in _EMPTY_CONFIG_VALUES:
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

    ``details[0]`` is the primary failure's ``FailureDetails``: the first failed
    check's typed ``error`` when present, else the first failed check's message
    wrapped in ``PreconditionError`` and stamped with the ``PREFLIGHT_FALLBACK_CODE``
    sentinel ``code`` (so the outcome event's ``reason`` marks an un-migrated block).
    ``details[1]`` carries every check so the red activity pane shows them — a failed
    activity has no result payload.
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
        details = (
            PreconditionError(
                message=fallback or "Preflight check failed",
                app_name=app_name,
                retryable=False,
            )
            .to_failure_details()
            .model_copy(update={"code": PREFLIGHT_FALLBACK_CODE})
        )

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


def _require_secret_store() -> Any:
    """Return the secret store, or raise so the gate fails open.

    A credential ref exists but there is no store to dereference it — an infra
    failure, not a valid empty-credential state. Raising routes to the workflow's
    fail-open path rather than calling the handler with empty creds and having a
    real block misattributed as AUTH.
    """
    infra = get_infrastructure()
    secret_store = infra.secret_store if infra is not None else None
    if secret_store is None:
        raise DependencyUnavailableError(
            message="No secret store available to resolve preflight credentials",
            service="secret_store",
        )
    return secret_store


async def _resolve_named_refs(
    input: PreflightGateInput,
) -> dict[str, list[HandlerCredential]]:
    """Resolve the app's named credential guids, grouped by ref name.

    One fail-open taxonomy, drawn from the resolver's own typed errors: a
    confirmed dependency outage (a ``CredentialVaultError`` wrapping a
    ``ColdStartRaceError``, or a ``DependencyUnavailableError``) propagates so
    the workflow fails open — a Dapr blip must never read as a bad credential and
    block a healthy run. Every other resolver failure becomes an empty group so
    the handler — not the gate — decides whether a missing credential is
    ``NOT_READY``: a genuine ``CredentialNotFoundError``, a plain
    ``CredentialVaultError``, or any unexpected vault error (the resolver
    collapses the latter two into ``CredentialNotFoundError``).
    """
    grouped: dict[str, list[HandlerCredential]] = {
        name: [] for name in input.credential_ref_fields
    }
    present = {
        name: guid
        for name, field in input.credential_ref_fields.items()
        if (guid := input.extraction_snapshot.get(field))
    }
    # A declared ref whose guid field is absent from the snapshot resolves to an
    # empty group — fail-open-safe. The log level distinguishes the two causes
    # (field names only, never secrets): some refs resolving and others absent is
    # most likely a typo in a guid field name (warn); every ref absent is almost
    # always a legitimate no-credential trigger, e.g. automation-trigger with
    # empty metadata (debug, so it doesn't warn on every such run).
    missing = {
        name: field
        for name, field in input.credential_ref_fields.items()
        if name not in present
    }
    if missing and present:
        logger.warning(
            "Some declared preflight credential ref(s) have no value in the "
            "extraction snapshot; verify the guid field names in "
            "preflight_credential_refs",
            missing_refs=missing,
        )
    elif missing:
        logger.debug(
            "All declared preflight credential refs are absent from the extraction "
            "snapshot; resolving to empty groups",
            missing_refs=missing,
        )
    if not present:
        return grouped

    resolver = CredentialResolver(_require_secret_store())
    for name, guid in present.items():
        ref = CredentialRef(name=name, credential_type="unknown", credential_guid=guid)
        try:
            raw = await resolver.resolve_raw(ref) or {}
        except CredentialNotFoundError:
            raw = {}
        grouped[name] = HandlerCredential.list_from_raw(raw)
    return grouped


async def _resolve_gate_credentials(
    input: PreflightGateInput,
) -> tuple[list[HandlerCredential], dict[str, list[HandlerCredential]]]:
    """Resolve credentials for the gate, in the activity frame.

    Returns ``(credentials, credentials_by_name)``. Apps that declare
    ``credential_ref_fields`` take the named path (``credentials`` stays empty,
    handlers read ``credentials_by_name``); every other app takes the unchanged
    single-triple path (any resolution error propagates → the workflow fails
    open, exactly as before this envelope carried named refs).
    """
    if input.credential_ref_fields:
        return [], await _resolve_named_refs(input)
    ref = CredentialRef.resolve_or_none(input)
    if ref is None:
        return [], {}
    raw = await CredentialResolver(_require_secret_store()).resolve_raw(ref) or {}
    return HandlerCredential.list_from_raw(raw), {}


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
        credentials, credentials_by_name = await _resolve_gate_credentials(input)
        # Build form config from the extraction-input snapshot in the activity
        # frame so app field reads stay outside the deterministic workflow.
        metadata_dump = _config_from_snapshot(
            input.extraction_snapshot, input.credential_ref_fields.values()
        )
        preflight_input = PreflightInput(
            credentials=credentials,
            credentials_by_name=credentials_by_name,
            entrypoint=input.entrypoint,
            metadata=BaseMetadataConfig(**metadata_dump),
            connection_config=BaseConnectionConfig(**metadata_dump),
            # The enforced per-attempt budget, so a handler that sizes its checks
            # to input.timeout_seconds stays inside the real deadline (not the 60s
            # default, which would silently time out to no_verdict).
            timeout_seconds=int(GATE_START_TO_CLOSE.total_seconds()),
        )
        # Redact every resolved secret from logs — the single-triple list and
        # every named group, without assuming which path populated which.
        all_creds = [
            *credentials,
            *(c for group in credentials_by_name.values() for c in group),
        ]
        with bind_invocation_context(app_name, all_creds):
            result = await handler.preflight_check(preflight_input)
        entry = input.entrypoint or "<implicit>"
        # The outcome event is the gate's queryable row (connector-pulse builds the
        # dashboard from it). The activity holds the verdict, so it emits the
        # proceeded/blocked rows; the workflow emits only no_verdict (fail-open).
        # ``reason`` is the status on proceed, the primary FailureDetails.code on a
        # block. Activity execution is at-least-once, so a retry after a lost
        # completion can re-emit — consumers dedupe on (workflow_run_id, outcome).
        if result.status is PreflightStatus.NOT_READY:
            block_error = _build_block_error(result, app_name)
            logger.info(
                PREFLIGHT_OUTCOME_EVENT,
                outcome="blocked",
                reason=block_error.details[0].code,
                app_name=app_name,
                entrypoint=entry,
                checks=len(result.checks),
            )
            raise block_error
        logger.info(
            PREFLIGHT_OUTCOME_EVENT,
            outcome="proceeded",
            reason=result.status.value,
            app_name=app_name,
            entrypoint=entry,
            checks=len(result.checks),
        )
        return result

    return preflight_gate
