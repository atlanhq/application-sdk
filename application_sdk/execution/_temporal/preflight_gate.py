"""Injected pre-extraction preflight gate (HYP-1883).

A core SDK extraction-lifecycle activity. Every generated workflow's ``_run``
dispatches ``{app}:preflight`` as its first step (see
:func:`application_sdk.app.base._run_preflight_gate`). ``READY`` and ``PARTIAL``
return normally.

The activity raises ``ApplicationError(type="PreflightFailed")`` — red in Temporal,
attributed to preflight — for anything it can attribute to the **source** while the
app is in hard mode: a ``NOT_READY`` verdict, a probe overrunning the budget, a
handler crash, or a provably absent credential. Failures of the gate's own
**plumbing** propagate instead, so the workflow fails open in either mode; a
platform blip must never fail a healthy run. ``_is_gate_broken`` draws that line
off the raised error's own ``FailureCategory`` (CNCT-99).

Enforcement lives here rather than in the workflow because this is the only frame
holding the resolved posture, and because the activity bounds the handler itself —
if Temporal's ``start_to_close`` killed it first there would be no frame left to
classify the failure in. Credential resolution also happens *inside* the activity;
the deterministic workflow only forwards the secret-free
:class:`PreflightGateInput`.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import BaseModel, Field, ValidationError
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import TimeoutType

with workflow.unsafe.imports_passed_through():
    from application_sdk.contracts.base import SerializableEnum
    from application_sdk.credentials.errors import CredentialNotFoundError
    from application_sdk.credentials.ingress import normalize_agent_json
    from application_sdk.credentials.ref import CredentialRef, CredentialResolvable
    from application_sdk.credentials.resolver import CredentialResolver
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.errors.base import AppError, sanitize_cause_repr
    from application_sdk.errors.categories import FailureCategory
    from application_sdk.errors.leaves import (
        AppTimeoutError,
        DependencyUnavailableError,
        InternalError,
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

    # Stable log bodies for the gate's two events — the contract connector-pulse
    # queries on. They live in the shared event-name registry and their values are
    # **unchanged** by the move there, so every dashboard string keeps working.
    # The outcome event is the gate's per-run verdict row; the posture event fires
    # once per gate-registered app at worker build and is the *denominator* the
    # outcome events cannot supply — an app that never reaches a verdict emits no
    # outcome row carrying ``gate_mode``, so "which apps believe they are gated" is
    # unanswerable from outcomes alone, and that is exactly the app whose broken
    # guarantee we most need to find.
    from application_sdk.observability.events import (
        PREFLIGHT_CHECK_EVENT,
        PREFLIGHT_OUTCOME_EVENT,
        PREFLIGHT_POSTURE_EVENT,
    )
    from application_sdk.observability.logger_adaptor import (
        CHECK_MATRIX_KEY,
        GATE_ATTEMPTS_KEY,
        GATE_CLASSIFICATION_KEY,
        GATE_DURATION_KEY,
        GATE_MODE_KEY,
        GATE_TIMEOUT_KEY,
        PREFLIGHT_SURFACE_KEY,
        AtlanLoggerAdapter,
        get_logger,
    )

logger = get_logger(__name__)

PREFLIGHT_FAILED_ERROR_TYPE = "PreflightFailed"

# Contract sentinel stamped as the primary FailureDetails.code on a fallback block
# (a handler that returned NOT_READY without a typed check error). It replaces the
# generic PRECONDITION code so the outcome event's ``reason`` distinguishes an
# un-migrated block from a typed one (whose reason is the handler error's own code,
# e.g. AUTH). category/audience/retryable are unchanged.
PREFLIGHT_FALLBACK_CODE = "PREFLIGHT_CHECK_FAILED"

# The check matrix for an outcome where no check ran — a skipped gate, or a
# fail-open the workflow reports without ever seeing the activity's result.
# Emitted rather than omitted so ``check_matrix`` is present on *every* outcome:
# a consumer can then parse it unconditionally instead of branching on presence,
# and a branch mishandled in the dropping direction is how a gate that never
# reached a verdict vanishes from the numerator it belongs in.
EMPTY_CHECK_MATRIX = "[]"


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


def underlying_error_type(exc: BaseException) -> str:
    """The class name of the underlying fault, seen through Temporal's
    ActivityError/ApplicationError wrapping.

    Temporal's default failure converter records a raised error as an
    ``ApplicationError`` whose ``type`` is the original class name, then wraps
    it in an ``ActivityError``. On the workflow side ``type(exc).__name__`` is
    therefore the wrapper ("ActivityError"), not the fault. The first *string*
    ``type`` in the cause chain is the real reason — the same chain
    :func:`is_preflight_block` walks.

    A deadline overrun needs its own answer, because Temporal reports it with a
    ``TimeoutError`` whose ``type`` is a ``TimeoutType`` *enum*, not a string. A
    string ``type`` still wins outright when one exists anywhere in the chain: a
    ``schedule_to_close`` expiry hangs the last attempt's ``ApplicationError``
    off the ``TimeoutError``, and that attempt's real fault (e.g.
    ``DaprSidecarUnreachableError``) is a better reason than the deadline that
    finally ended it. Only when the chain carries no string ``type`` at all does
    the timeout answer: ``Timeout:<TIMEOUT_TYPE>`` (e.g.
    ``Timeout:START_TO_CLOSE``). Naming *which* deadline fired is the point —
    ``START_TO_CLOSE`` says one attempt outran its own budget, and per CONNECT-841
    that is what a Dapr cold-start wait wider than the gate's ``start_to_close``
    looks like from the workflow; ``SCHEDULE_TO_CLOSE`` says the retry window
    closed. Reporting the wrapper name ("ActivityError") for either — as this did
    before — is the same uninformative label this function exists to remove.

    Falls back to the top-level class name only when the chain offers neither
    (e.g. a bare ``ApplicationError`` with no ``type`` set, or a plain
    ``RuntimeError``). Every return is a ``str``: ``reason`` is a field every
    consumer reads as a string, so an enum object must never reach it.

    Used for the fail-open ``no_verdict`` outcome row's ``reason`` so a
    persistent platform fault reaches the dashboard as its real cause (e.g.
    ``DaprSidecarUnreachableError``) instead of an uninformative wrapper name.
    """
    seen: set[int] = set()
    timeout_reason: str | None = None
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        found = getattr(current, "type", None)
        if isinstance(found, str) and found:
            return found
        if timeout_reason is None and isinstance(found, TimeoutType):
            timeout_reason = f"Timeout:{found.name}"
        nxt = getattr(current, "cause", None)
        current = nxt if nxt is not None else current.__cause__
    return timeout_reason or type(exc).__name__


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
            # Normalised, not passed through: a custom input may still carry the
            # raw wire value (a JSON string, or the marketplace-package
            # placeholder that fails typed validation). Before this, such a value
            # failed the construction below and cost the gate its
            # extraction_method and credential_guid too — degrading credential
            # resolution silently instead of just having no agent reference.
            agent_json=normalize_agent_json(getattr(input_data, "agent_json", None)),
            credential_ref=getattr(input_data, "credential_ref", None),
            entrypoint=entrypoint,
            credential_ref_fields=credential_ref_fields,
            extraction_snapshot=snapshot,
        )
        try:
            return cls(**base_kw)
        except ValidationError as exc:
            # Drop only the fields that actually failed, so one oddly-shaped
            # field cannot cost the gate its routing triple (extraction_method /
            # credential_guid / entrypoint) — without those the gate resolves the
            # wrong credential, or none, and reports a fail-open verdict nobody
            # can trace back to this line.
            rejected = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
            logger.warning(
                "Extraction input did not fit PreflightGateInput; dropping the "
                "rejected field(s) %s and keeping the rest",
                sorted(rejected),
                exc_info=True,
            )
            kept = {k: v for k, v in base_kw.items() if k not in rejected}
            try:
                return cls(**kept)
            except ValidationError:
                logger.warning(
                    "Extraction input still did not fit PreflightGateInput; using "
                    "a minimal gate input so the gate still runs",
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


GATE_RETRY = RetryPolicy(maximum_attempts=2, backoff_coefficient=2)

# The handler's check budget, in seconds. Per-app via ``App.preflight_gate_timeout_seconds``
# and clamped by ``resolve_gate_budget_seconds`` — a slow source is an app-specific
# fact, so a fleet-wide bump is the wrong lever (it costs every app's
# time-to-first-activity). The ceiling keeps an app from stalling extraction start
# indefinitely; the floor keeps a typo from leaving no time to run any check.
# 150s, not 25s. The budget is a deadline, not a reservation: a handler that
# returns in 3s holds the slot for 3s whatever the budget says. Measured p95 across
# the fleet is under 14s for every app but one federated source (~124s), so raising
# it changes nothing on a healthy run. What it changes is the *pathological* run —
# it now has time to reach a real verdict instead of being cut at 25s, which is how
# a censored measurement became a fail-open. 150 covers the slowest observed p95
# with headroom while leaving the 300s ceiling meaningful for an app that needs
# more. Expected to come down once a few weeks of gate_duration_ms exist.
GATE_TIMEOUT_DEFAULT_SECONDS = 150
GATE_TIMEOUT_MIN_SECONDS = 5
# 300s ceiling: headroom above the slowest observed source, so an owner who needs
# more than the default can still declare it without an SDK change.
GATE_TIMEOUT_MAX_SECONDS = 300

# Retry attempts for the gate activity, per-app via ``App.preflight_gate_max_attempts``.
# A second attempt rescues a *transient* slow probe (cold pool, cluster resuming);
# it cannot rescue a systematically slow one, it just doubles time-to-verdict. So
# an app declaring a large budget should usually pair it with one attempt.
GATE_ATTEMPTS_DEFAULT = 2
GATE_ATTEMPTS_MIN = 1
GATE_ATTEMPTS_MAX = 3

# Slack between the budget the handler gets and Temporal's start_to_close, so the
# gate's own ``asyncio.wait_for`` always fires first. If Temporal won the race the
# activity would be killed before its ``except`` ran, losing both the classification
# and the mode decision — that is the CNCT-99 defect.
GATE_ACTIVITY_HEADROOM_SECONDS = 5

# Floor on what's left after credential resolution. Below this there is no point
# calling the handler: resolution has eaten the budget, which is a plumbing
# problem, not evidence about the source. Without this floor a slow vault would
# hand the handler a sliver of budget, the handler would time out, and the
# resulting block would blame the source for the secret store being slow.
GATE_MIN_HANDLER_SECONDS = 1.0


def _min_handler_seconds(budget_seconds: float) -> float:
    """The floor, capped at half the budget.

    The floor exists to reject a *sliver* of remaining budget. It must never
    exceed half of what was granted, or it would reject budgets it was meant to
    accept and turn every run into a fail-open.
    """
    return min(GATE_MIN_HANDLER_SECONDS, budget_seconds / 2)


# Why the gate could not reach a verdict. Stamped on the outcome event so
# connector-pulse can separate "we know we couldn't ask the source" from "our own
# plumbing broke" — only the former is subject to gate mode.
CLASSIFICATION_SOURCE_UNVERIFIABLE = "source_unverifiable"
CLASSIFICATION_GATE_BROKEN = "gate_broken"

# The handler returned a real verdict. Stamped explicitly rather than left absent
# so consumers key on a value: "field missing" would otherwise have to mean both
# "this was a genuine verdict" and "this row predates the classification".
CLASSIFICATION_VERDICT = "verdict"

# Who must act, stamped on the outcome row (FND-901). Same key and values the log
# interceptor projects from raised AppErrors, riding the OTel ``failure.``
# passthrough — but stamped here explicitly, because the row is emitted before
# the block is raised and would otherwise carry no audience.
FAILURE_AUDIENCE_KEY = "failure.audience"

# Synthetic check name for a no-verdict outcome, so the check matrix and the red
# activity pane carry a row rather than showing zero checks.
UNVERIFIABLE_CHECK_NAME = "preflightVerdict"

# Error type for a retryable no-verdict on a non-final attempt. Deliberately not
# PREFLIGHT_FAILED_ERROR_TYPE: the workflow must not treat it as the deliberate
# block and abort before the retry has had its turn.
PREFLIGHT_NO_VERDICT_ERROR_TYPE = "PreflightNoVerdict"

# ``FailureCategory`` already draws the line this gate needs: DEPENDENCY_UNAVAILABLE
# is documented as Atlan-internal platform services while SOURCE_UNAVAILABLE is the
# customer's own system (see errors/categories.py). RATE_LIMITED joins the plumbing
# side because a 429 means "ask me later", not "the source is not ready" — collapsing
# it into a verdict would make hard mode fail *closed* on a transient.
_GATE_BROKEN_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        FailureCategory.RATE_LIMITED,
        FailureCategory.RESOURCE_EXHAUSTED,
        FailureCategory.CANCELLED,
    }
)


def log_gate_posture(app_name: str, *, enforce: bool, budget_seconds: int) -> None:
    """Emit the queryable boot-time posture row for one gate-registered app.

    Emitted for **every** gate app, soft included — the point is a complete
    denominator. Ranking hard-mode apps that never produce a verdict needs the set
    of apps declaring hard mode, and soft rows are what make adoption and posture
    drift measurable rather than a code-search artifact.

    Separate from the human-facing hard-mode boot warning by design: this body is a
    pinned contract string that must never be reworded, that one is prose an
    operator reads.
    """
    logger.info(
        PREFLIGHT_POSTURE_EVENT,
        app_name=app_name,
        **{
            GATE_MODE_KEY: "hard" if enforce else "soft",
            GATE_TIMEOUT_KEY: budget_seconds,
        },
    )


def _clamp_declared_int(
    raw: Any, *, low: int, high: int, default: int, unit: str
) -> tuple[int, str]:
    """Coerce and clamp a declared ``ClassVar`` int. Returns ``(value, complaint)``.

    Pure and silent so the warn-once boot path and the per-run workflow path can
    share it and cannot disagree about the resulting number. ``complaint`` is
    empty when the declaration was already valid.
    """
    if raw is None:
        return default, ""
    # bool is an int subclass; True would otherwise clamp to the floor and read
    # as a deliberate declaration.
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return default, f"{raw!r} is not a number"
    try:
        value = int(float(raw))
    # OverflowError, not ValueError, for inf / "1e400" — and this runs on the
    # workflow path, where an escaping exception becomes a workflow *task*
    # failure that Temporal retries indefinitely.
    except (TypeError, ValueError, OverflowError):
        return default, f"{raw!r} is not a usable number"
    clamped = max(low, min(high, value))
    if clamped != value:
        return (
            clamped,
            f"{value}{unit} is outside the supported {low}-{high}{unit} range",
        )
    return clamped, ""


def _clamp_budget(raw: Any) -> tuple[int, str]:
    return _clamp_declared_int(
        raw,
        low=GATE_TIMEOUT_MIN_SECONDS,
        high=GATE_TIMEOUT_MAX_SECONDS,
        default=GATE_TIMEOUT_DEFAULT_SECONDS,
        unit="s",
    )


def _clamp_attempts(raw: Any) -> tuple[int, str]:
    return _clamp_declared_int(
        raw,
        low=GATE_ATTEMPTS_MIN,
        high=GATE_ATTEMPTS_MAX,
        default=GATE_ATTEMPTS_DEFAULT,
        unit="",
    )


def resolve_gate_budget_seconds(raw: Any) -> int:
    """Resolve an app's declared gate budget, warning once about a bad value.

    Called at worker build, where a complaint about a malformed declaration is
    worth a log line. Never raises — a bad value must not stop a worker booting,
    it just falls back to the default.
    """
    budget, complaint = _clamp_budget(raw)
    if complaint:
        logger.warning(
            "preflight_gate_timeout_seconds: %s; using %ds", complaint, budget
        )
    return budget


def resolve_gate_attempts(raw: Any) -> int:
    """Resolve an app's declared gate attempts, warning once about a bad value."""
    attempts, complaint = _clamp_attempts(raw)
    if complaint:
        logger.warning("preflight_gate_max_attempts: %s; using %d", complaint, attempts)
    return attempts


def gate_retry_policy(attempts: Any) -> RetryPolicy:
    """The gate's retry policy for one app, from its declared attempts."""
    resolved, _ = _clamp_attempts(attempts)
    return RetryPolicy(maximum_attempts=resolved, backoff_coefficient=2)


def gate_timeouts(
    budget_seconds: Any, attempts: Any = None
) -> tuple[timedelta, timedelta]:
    """Derive ``(start_to_close, schedule_to_close)`` from the budget and attempts.

    Clamps its own inputs so the workflow can size activity timeouts without
    re-warning on every run (the worker already complained once at boot) and
    still lands on the same numbers the activity was built with.

    Both timeouts must move together with the budget. ``start_to_close`` adds
    headroom so the activity's own timeout wins the race; ``schedule_to_close``
    fits every retry attempt, otherwise the retry policy is cosmetic — the second
    attempt could not start before the schedule cap fired. It also tracks
    ``attempts``: a one-attempt app must not reserve a two-attempt window, or it
    holds its slot for twice as long as its owner asked for.
    """
    budget, _ = _clamp_budget(budget_seconds)
    resolved_attempts, _ = _clamp_attempts(attempts)
    start_to_close = budget + GATE_ACTIVITY_HEADROOM_SECONDS
    # +10s absorbs the retry backoff between attempts.
    schedule_to_close = resolved_attempts * start_to_close + 10
    return timedelta(seconds=start_to_close), timedelta(seconds=schedule_to_close)


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


def _check_matrix_json(checks: list[PreflightCheck]) -> str:
    """Compact per-check matrix for the outcome event, as one JSON string.

    Lands as a single ``LogAttributes`` value in ClickHouse, so connector-pulse
    can pattern-match verdicts against workflow outcomes (``JSONExtract``) with
    no schema change. Small fixed fields only — messages and evidence stay in
    the Temporal activity result. Blocking intent is not a per-check field: it
    is observable from the outcome itself (``would_block``/``blocked`` means
    the aggregate was NOT_READY; a failed check on a ``proceeded`` run is
    advisory by the handler's own choice).
    """
    rows = []
    for check in checks:
        rows.append(
            {
                "name": check.name,
                "passed": check.passed,
                "error_code": check.error.code if check.error else "",
                # orjson emits null for nan/inf; we normalize to 0.0 so the
                # ClickHouse row stays numeric for downstream JSONExtract, and
                # never raise — a raise here fails the gate open and loses the
                # whole event.
                "duration_ms": check.duration_ms
                if math.isfinite(check.duration_ms)
                else 0.0,
            }
        )
    return orjson.dumps(rows).decode()


def _build_block_error(result: PreflightOutput, app_name: str) -> Any:
    """Build the ``PreflightFailed`` ApplicationError for a NOT_READY verdict.

    ``details[0]`` is the primary failure's ``FailureDetails``: the handler's typed
    aggregate ``result.error`` when present, else the first failed check's typed
    ``error``, else the first failed check's message wrapped in ``PreconditionError``
    and stamped with the ``PREFLIGHT_FALLBACK_CODE`` sentinel ``code`` (so the
    outcome event's ``reason`` marks an un-migrated block). ``details[1]`` carries
    every check so the red activity pane shows them — a failed activity has no
    result payload.
    """
    from application_sdk.execution.errors import ApplicationError  # noqa: PLC0415

    failed = [c for c in result.checks if not c.passed]
    # Prefer the handler's typed aggregate error. It is the reason the verdict is
    # NOT_READY and stays pinned to the real cause even when a non-fatal row is
    # inserted ahead of it in ``checks``; the per-check scan is the fallback for
    # handlers that set only ``checks``.
    primary_error = result.error or next(
        (c.error for c in failed if c.error is not None), None
    )
    if primary_error is not None:
        details = primary_error
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
        result.resolved_message
        or joined
        or "Preflight check failed; aborting before extraction"
    )
    checks_payload = {"checks": [_dump_check(c) for c in result.checks]}
    return ApplicationError(
        f"Preflight failed: {reason}",
        details,
        checks_payload,
        type=PREFLIGHT_FAILED_ERROR_TYPE,
        non_retryable=True,
    )


class PreflightSurface(SerializableEnum):
    """Which surface ran ``Handler.preflight_check`` outside a gated run.

    Stamped as ``preflight_surface`` on the interactive outcome row, and the
    input to the level policy below — so this is an enumerated vocabulary, not
    free text. Values are the wire strings already shipped in that attribute
    and must not be reworded: dashboards filter on them.
    """

    #: The ``/workflows/v1/check`` endpoint behind the setup form.
    HTTP = "http"
    #: The ``sdr:preflight_check`` Temporal activity (test-connection).
    SDR = "sdr"


#: Per surface: is the outcome row the customer's *only* sight of the verdict?
#:
#: The level policy turns on this, not on how expected the verdict is. HTTP
#: returns the verdict as the response body the setup form renders, so its row
#: is a duplicate and stays INFO. An SDR failure travels back through a workflow
#: whose run log the customer reads at the default ERROR filter, so that row
#: mirrors the gate's levels or the failure is invisible — the hole FND-901
#: exists to close.
#:
#: A table rather than a branch so the policy is *enumerable*:
#: ``test_every_surface_has_a_level_policy`` asserts these keys cover
#: ``PreflightSurface``, which fails CI for a new member nobody routed. An
#: exhaustive ``if``/``assert_never`` would only warn here — this repo sets
#: ``reportArgumentType = "warning"``, so pyright flags the gap without failing
#: on it.
_LOG_ROW_IS_ONLY_CHANNEL: dict[PreflightSurface, bool] = {
    PreflightSurface.HTTP: False,
    PreflightSurface.SDR: True,
}


def _log_row_is_only_channel(surface: PreflightSurface) -> bool:
    """Look up ``surface``'s level policy, defaulting an unrouted one to loud.

    Never raises on a miss: this is the emit path, and losing the row entirely
    is strictly worse than logging it one level too loud. The test above is what
    keeps the miss from happening.
    """
    return _LOG_ROW_IS_ONLY_CHANNEL.get(surface, True)


def emit_preflight_check_outcome(
    log: AtlanLoggerAdapter,
    app_name: str,
    result: PreflightOutput,
    *,
    surface: PreflightSurface,
    entrypoint: str | None = None,
    request_id: str | None = None,
) -> None:
    """Emit the interactive-surface sibling of the gate's outcome row (FND-901).

    One attribute schema for every surface that runs ``Handler.preflight_check``,
    so the setup funnel (HTTP form check, SDR test-connection) is queryable next
    to run-time gate verdicts. The level follows whether the log is the delivery
    channel — see :func:`_log_row_is_only_channel`. A surface whose row is the
    only channel mirrors the gate's map (``not_ready`` at ERROR, a passed
    verdict carrying a failed advisory check at WARNING, clean at INFO); one
    that returns the verdict by another route stays INFO throughout. Handler
    crashes are logged at ERROR by each surface's own boundary handler. Callers
    pass their module logger so the row keeps the surface's source.
    """
    failed = [c for c in result.checks if not c.passed]
    # The aggregate error wins over check order, mirroring _build_block_error:
    # SDR inserts a non-fatal secret-store row ahead of the real failure and
    # pins the real one on result.error — first-failed would steal the banner.
    primary = result.error or next(
        (c.error for c in failed if c.error is not None), None
    )
    reason = result.status.value
    if result.status is PreflightStatus.NOT_READY:
        reason = primary.code if primary is not None else PREFLIGHT_FALLBACK_CODE
    extra: dict[str, Any] = {}
    if primary is not None:
        extra[FAILURE_AUDIENCE_KEY] = primary.audience.value
    if request_id is not None:
        extra["request_id"] = request_id
    if not _log_row_is_only_channel(surface):
        emit = log.info
    elif result.status is PreflightStatus.NOT_READY:
        emit = log.error
    elif failed:
        emit = log.warning
    else:
        emit = log.info
    emit(
        PREFLIGHT_CHECK_EVENT,
        outcome=result.status.value,
        reason=reason,
        app_name=app_name,
        entrypoint=entrypoint or "<implicit>",
        checks=len(result.checks),
        **{
            CHECK_MATRIX_KEY: _check_matrix_json(result.checks),
            PREFLIGHT_SURFACE_KEY: surface.value,
        },
        **extra,
    )


def _is_gate_broken(exc: BaseException) -> bool:
    """Whether ``exc`` is the gate's own plumbing failing, not source evidence.

    Routed off the raised error's own ``FailureCategory`` rather than a list of
    exception classes, so an app raising any typed SDK error lands on the right
    side without the gate knowing about it. An untyped exception is *not* treated
    as plumbing: a handler crash is an app fault the gate can attribute, and
    defaulting it to fail-open is what let hard mode mean nothing.
    """
    return isinstance(exc, AppError) and type(exc).category in _GATE_BROKEN_CATEGORIES


def _effective_budget(budget_seconds: float) -> float:
    """The handler budget, capped by the deadline Temporal actually enforces.

    The worker builds the activity with the app's budget while the workflow sizes
    ``start_to_close`` from the same ``ClassVar`` — two independent reads that can
    skew during a rolling deploy, or when the worker cannot resolve the app class
    and falls back to the default. If the workflow's deadline turns out to be the
    tighter one, Temporal would kill the activity before its own timeout fired and
    the classification would be lost. Reading the real deadline back off
    ``activity.info()`` makes "the gate's own timeout wins" true by construction.
    """
    try:
        start_to_close = activity.info().start_to_close_timeout
        if not isinstance(start_to_close, timedelta):
            return budget_seconds
        ceiling = start_to_close.total_seconds() - GATE_ACTIVITY_HEADROOM_SECONDS
        return min(budget_seconds, ceiling) if ceiling > 0 else budget_seconds
    except RuntimeError:
        # No activity context (direct call, unit test) — expected, not notable.
        return budget_seconds
    # This function exists to stop a budget skew from breaking a run, so it must
    # never become the thing that breaks one; anything unexpected degrades to the
    # declared budget.
    except Exception:
        logger.debug(
            "Could not read the activity's start_to_close; using the declared "
            "preflight budget of %ss",
            budget_seconds,
            exc_info=True,
        )
        return budget_seconds


def _is_definitive_credential_absence(exc: BaseException) -> bool:
    """Whether ``exc`` proves the credential is genuinely not there.

    The resolver deliberately collapses *any* unexpected vault error into
    ``CredentialNotFoundError`` (see ``CredentialResolver.resolve_raw``) so the
    handler, not the resolver, decides what a missing credential means. That is
    fine when the gate fails open on everything, but under gate mode it would
    turn a transport blip into "your credential is missing" and abort a healthy
    run in hard mode.

    So a not-found is only source-attributable when it is *provably* an absence:
    no cause at all, or a cause that is itself a definitive
    ``SecretNotFoundError``. Anything else is a collapsed plumbing failure and
    fails open.
    """
    from application_sdk.infrastructure.secrets import (  # noqa: PLC0415 — avoid import cycle at module load
        SecretNotFoundError,
    )

    if not isinstance(exc, CredentialNotFoundError):
        return False
    cause = exc.__cause__ or exc.cause
    return cause is None or isinstance(cause, SecretNotFoundError)


def _current_attempt() -> int:
    """This activity attempt, or 1 outside an activity context."""
    try:
        return activity.info().attempt
    except RuntimeError:  # not inside an activity context
        return 1


def _is_final_attempt(attempts: int) -> bool:
    """Whether this is the gate activity's last retry attempt.

    A slow source deserves the retries the app's policy grants, so a no-verdict
    only becomes a verdict once they are exhausted. Reads the app's own
    ``attempts`` rather than a module default: a one-attempt app has no retry to
    wait for, and deferring its verdict would mean never reaching one. Outside an
    activity context (direct calls, unit tests) the answer is ``True`` —
    enforcement must never be skipped just because the attempt is unknown.
    """
    try:
        attempt = activity.info().attempt
    except RuntimeError:  # not inside an activity context
        return True
    return attempt >= max(1, attempts)


def _unverifiable_result(exc: BaseException, app_name: str) -> PreflightOutput:
    """Build the ``NOT_READY`` verdict for a source the gate could not verify.

    Shaped as a normal handler verdict with one failed check so the existing
    block/emit machinery (``_build_block_error``, ``_check_matrix_json``) applies
    unchanged — a no-verdict outcome reports through the same surfaces as a real
    one instead of needing a parallel path.
    """
    if isinstance(exc, AppError):
        details = exc.to_failure_details()
        if details.app_name is None:
            details = details.model_copy(update={"app_name": app_name})
    else:
        # An untyped handler crash is an app fault, not a timeout — INTERNAL with
        # classification_pending is what the taxonomy has for "unclassified, needs
        # a typed leaf". Labelling it TIMEOUT would report every AttributeError to
        # the Automation Engine as a slow source.
        #
        # The raw message is sanitized, never interpolated: a driver exception
        # routinely carries the connection string (see credentials/errors.py), and
        # this text lands on FailureDetails.message, in Temporal history, and in
        # ClickHouse. to_failure_details() sanitizes cause_repr but not message.
        details = InternalError(
            message=f"Preflight could not be verified: {sanitize_cause_repr(exc)}",
            app_name=app_name,
            cause=exc,
            retryable=False,
            component="preflight_handler",
            classification_pending=True,
        ).to_failure_details()
    return PreflightOutput(
        status=PreflightStatus.NOT_READY,
        message=details.message,
        checks=[
            PreflightCheck(name=UNVERIFIABLE_CHECK_NAME, passed=False, error=details)
        ],
    )


# Floor below which the gate skips the storage probes rather than starting a
# check it has no time to finish. Skipping fails open on the storage dimension
# only — the handler's source verdict stands either way. 15s, not lower: a
# real cloud round-trip of write + HEAD + multipart initiate/part/complete
# against a cold pool can take several seconds per store, and a probe started
# with a sliver of budget would time out and report a healthy store as a
# connectivity failure — blocking a hard-mode run for its own starvation.
_STORAGE_CHECK_MIN_SECONDS = 15.0


def _storage_failure_details(result: Any, *, sdr_mode: bool) -> Any:
    """Map a failed storage probe onto typed ``FailureDetails`` for a gate check.

    In SDR mode the deployment store is the customer's own bucket, so the
    role-aware mapper the SDR surface already uses
    (``_object_store_failure`` in ``sdr.py``) owns attribution. In-cluster,
    every probed store is Atlan-side infrastructure — a failure there must
    never read as the customer's source being unready — so it maps to the
    PLATFORM-audience :class:`DependencyUnavailableError` unconditionally.

    A relocation-bucket failure gets ``StorageBucketRelocationError.code``
    stamped as its ``code`` in both modes, replacing the leaf's generic code
    the way ``PREFLIGHT_FALLBACK_CODE`` replaces PRECONDITION — one code for
    the gate block and the mid-run upload failure, read lazily from its single
    definition.
    """
    # Imported lazily (activity frame only) so the workflow-sandbox import set
    # never pulls the storage package (whose __init__ imports ops → obstore at
    # module load); the bucket name and the relocation code come from their
    # single definitions rather than copied literals, so a rename breaks
    # loudly here instead of silently dropping the code stamp.
    from application_sdk.storage.errors import (  # noqa: PLC0415 — activity frame only
        StorageBucketRelocationError,
    )
    from application_sdk.storage.preflight import (  # noqa: PLC0415 — activity frame only
        RELOCATION_BUCKET,
    )

    if sdr_mode:
        from application_sdk.execution._temporal.sdr import (  # noqa: PLC0415 — sdr imports this module at load; runtime-only reverse import
            _object_store_failure,
        )

        details = _object_store_failure(
            label=result.label,
            error_class=result.error_class,
            binding_name=result.binding_name,
            message=result.message,
            hint=result.hint,
        ).to_failure_details()
    else:
        details = DependencyUnavailableError(
            message=result.message,
            suggested_action=result.hint,
            service="object_store",
            target=result.binding_name,
            network_error=result.error_class or "connectivity / unknown",
        ).to_failure_details()
    if result.error_class == RELOCATION_BUCKET:
        details = details.model_copy(update={"code": StorageBucketRelocationError.code})
    return details


async def _append_storage_checks(
    result: PreflightOutput, budget_seconds: float, started_monotonic: float
) -> bool:
    """Fold run-path object-store probes into the handler's verdict.

    The handler certifies the *source*; nothing certifies the stores the run
    will upload its artifacts to — and a run that cannot upload is doomed
    however healthy the source is (a production RCA traced multi-hour
    extractions dying at the final upload to a store condition detectable here
    in under a second). Appends one check per configured store and downgrades a
    ``READY`` or ``PARTIAL`` verdict to ``NOT_READY`` when any store fails, so
    the existing block/emit machinery applies unchanged and the mode decision
    stays where it lives today.

    Deliberate taxonomy note: the gate fails **open** when its own plumbing
    *raises* (see ``_is_gate_broken``); this check instead returns a *verdict*
    that a confirmed storage failure — surviving the activity's retry policy —
    blocks a hard-mode run. A store that rejects every upload for hours is not
    the transient blip the fail-open exists to protect.

    Budget-bounded: runs only in the time left of the gate's own budget, so the
    activity's ``start_to_close`` (budget + headroom) still wins every race.
    Skipped entirely — storage unverified, verdict untouched — when the handler
    consumed too much of the budget, or when the handler already returned
    ``NOT_READY`` (the run is blocked or reported either way; spending more of
    the slot cannot change that). **Never raises**: any unexpected failure logs
    and leaves the handler's verdict as it was.

    Returns:
        ``True`` when at least one storage probe failed (the verdict was
        downgraded), so the caller can defer the block to the gate's retry
        policy on a non-final attempt — one flaky probe must not abort a
        hard-mode run on its first attempt.
    """
    try:
        if result.status is PreflightStatus.NOT_READY:
            return False
        remaining = budget_seconds - (time.monotonic() - started_monotonic)
        if remaining < _STORAGE_CHECK_MIN_SECONDS:
            logger.info(
                "Skipping run-path storage checks: %.1fs of the gate budget "
                "left (< %.1fs floor)",
                remaining,
                _STORAGE_CHECK_MIN_SECONDS,
            )
            # Visible, not a debug-level nothing: the owner opted in, so
            # 'storage verified clean' and 'storage never probed' must be
            # distinguishable in the check matrix (and to connector-pulse).
            # Failed-but-not-downgrading = advisory; the verdict stands.
            result.checks.append(
                PreflightCheck(
                    name="objectStoreAccess:skipped",
                    passed=False,
                    message=(
                        "Storage was not verified: the handler left "
                        f"{remaining:.1f}s of the gate budget, under the "
                        f"{_STORAGE_CHECK_MIN_SECONDS:.0f}s the checks need"
                    ),
                )
            )
            return False
        from application_sdk.constants import (  # noqa: PLC0415 — read at call time so tests and SDR toggles see the live value
            ENABLE_ATLAN_UPLOAD,
        )
        from application_sdk.storage.preflight import (  # noqa: PLC0415 — activity frame only; keep obstore out of the workflow-sandbox import set
            check_run_storage_access,
        )

        probes = await check_run_storage_access(
            get_infrastructure(), timeout_seconds=remaining - 1.0
        )
        if not probes:
            return False
        first_failed_details = None
        for probe in probes:
            if probe.passed:
                result.checks.append(
                    PreflightCheck(
                        name=f"objectStoreAccess:{probe.label}",
                        passed=True,
                        message=probe.message,
                    )
                )
                continue
            details = _storage_failure_details(probe, sdr_mode=ENABLE_ATLAN_UPLOAD)
            if first_failed_details is None:
                first_failed_details = details
            result.checks.append(
                PreflightCheck(
                    name=f"objectStoreAccess:{probe.label}",
                    passed=False,
                    message=probe.message,
                    error=details,
                )
            )
        # READY *and* PARTIAL downgrade: both proceed today, and a run that
        # cannot upload its artifacts is doomed whichever of the two the
        # handler returned. Pin the aggregate ``result.error`` to the first
        # failed store (the same pinning the SDR downgrade does) — without it
        # ``_build_block_error`` would prefer a failed *advisory* handler
        # check's error, and the block would be attributed to the source
        # instead of storage.
        if first_failed_details is not None:
            if result.status is not PreflightStatus.NOT_READY:
                result.status = PreflightStatus.NOT_READY
            # Unconditional: this downgrade is why the verdict is NOT_READY,
            # and ``PreflightOutput.error`` is defined as exactly that reason.
            # A conditional pin would let a handler-set aggregate on a PARTIAL
            # verdict steal the block banner from the storage failure.
            result.error = first_failed_details
            return True
        return False
    except Exception:
        logger.warning(
            "Run-path storage checks could not run; proceeding without "
            "storage verification",
            exc_info=True,
        )
        return False


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
    *,
    enforce: bool = False,
    budget_seconds: float = GATE_TIMEOUT_DEFAULT_SECONDS,
    attempts: int = GATE_ATTEMPTS_DEFAULT,
    verify_storage: bool = False,
) -> Callable[..., Awaitable[Any]]:
    """Build the injected preflight-gate activity (``{app}:preflight``).

    ``verify_storage`` is the per-app opt-in (``App.preflight_verify_storage``)
    to also probe the run's artifact object store(s) after the handler's source
    verdict — see :func:`_append_storage_checks`.

    Registered unconditionally by the worker (independent of the SDR opt-out)
    because the gate is mandatory. Binds the same per-invocation handler context
    the HTTP and SDR paths use (:func:`bind_invocation_context`).

    ``enforce`` is the gate's posture for this app, resolved once at worker
    build (see ``_resolve_gate_enforcement`` in the worker). Soft (``False``,
    the default) never raises: the verdict stays honest ``NOT_READY``, the run
    proceeds, and the dodged block is emitted as ``outcome="would_block"`` so
    connector-pulse can rank apps whose checks would have blocked real runs.
    Hard (``True``) is the per-app opt-in: it raises and aborts the run. The
    handler is never consulted about posture — verdict and enforcement are
    deliberately separate concerns.

    ``budget_seconds`` is the handler's check budget, already clamped by
    :func:`resolve_gate_budget_seconds`. The activity enforces it itself rather
    than trusting the handler to self-police: it is stamped on
    ``PreflightInput.timeout_seconds`` *net of credential resolution* and also
    applied as an ``asyncio.wait_for``. Enforcing here is what makes mode
    applicable at all — if Temporal's timeout killed the activity first, there
    would be no frame left to classify the failure or consult ``enforce``.

    Note the one case this cannot cover: ``wait_for`` only cancels at an await
    point, so a handler doing blocking synchronous I/O on the event loop is not
    interrupted, and that run still falls through to the workflow's mode-blind
    fail-open. Handlers must keep their probes awaitable.
    """

    @activity.defn(name=preflight_gate_activity_name(app_name))
    async def preflight_gate(input: PreflightGateInput) -> PreflightOutput:
        entry = input.entrypoint or "<implicit>"
        gate_mode = "hard" if enforce else "soft"

        def _emit_outcome(
            outcome: str,
            reason: str,
            checks: list[PreflightCheck],
            classification: str,
            audience: str | None = None,
            exc_info: BaseException | None = None,
        ) -> None:
            """Emit the gate's one queryable row.

            Single site for all three activity-side outcomes so the attribute set
            cannot drift between them — a consumer that finds ``gate_duration_ms``
            on ``proceeded`` but not on ``would_block`` cannot compute headroom
            for the runs that need it most.

            The row is also the level carrier (FND-901): the customer-facing log
            view filters at ERROR, so a ``blocked`` run — and an unverifiable
            source, whose failure is real in both modes — must be the ERROR
            record itself, not a WARN beside one. A ``proceeded`` run carrying a
            failed check is the advisory case WARNING is semantically for
            (P047 bans the handler from logging it, so the gate must). Keyed on
            the checks rather than ``PreflightStatus.PARTIAL`` because PARTIAL
            is documented display-only — a handler may return READY with a
            failed advisory row. Clean proceeds, skips and soft-mode verdict
            ``would_block`` stay INFO.

            ``gate_duration_ms`` is measured here rather than summed from
            ``check_matrix``: per-check durations are handler-authored and a
            handler abandoned at ``start_to_close`` keeps running and reports a
            duration past the budget.
            """
            if (
                outcome == "blocked"
                or classification == CLASSIFICATION_SOURCE_UNVERIFIABLE
            ):
                emit = logger.error
            elif outcome == "proceeded" and any(not c.passed for c in checks):
                emit = logger.warning
            else:
                emit = logger.info
            extra: dict[str, Any] = {}
            if audience is not None:
                extra[FAILURE_AUDIENCE_KEY] = audience
            if exc_info is not None:
                extra["exc_info"] = exc_info
            emit(
                PREFLIGHT_OUTCOME_EVENT,
                outcome=outcome,
                reason=reason,
                app_name=app_name,
                entrypoint=entry,
                checks=len(checks),
                **{
                    CHECK_MATRIX_KEY: _check_matrix_json(checks),
                    GATE_MODE_KEY: gate_mode,
                    GATE_CLASSIFICATION_KEY: classification,
                    GATE_DURATION_KEY: round((time.monotonic() - started) * 1000, 1),
                    GATE_TIMEOUT_KEY: int(budget_seconds),
                    GATE_ATTEMPTS_KEY: _current_attempt(),
                },
                **extra,
            )

        def _no_verdict(exc: BaseException) -> PreflightOutput:
            """Apply gate mode to a source we could not verify.

            Raises the deliberate block in hard mode, returns the honest
            ``NOT_READY`` in soft. Plumbing failures never reach here — they
            propagate to the workflow's fail-open.
            """
            if not _is_final_attempt(attempts):
                # Let the app's retry policy have its turn; a slow source may
                # answer next attempt. Not the block error type, so the workflow
                # does not abort on a first slow attempt.
                from application_sdk.execution.errors import (  # noqa: PLC0415 — avoid import cycle at module load
                    ApplicationError,
                )

                raise ApplicationError(
                    "Preflight could not reach a verdict: "
                    f"{sanitize_cause_repr(exc)}",
                    type=PREFLIGHT_NO_VERDICT_ERROR_TYPE,
                )
            unverifiable = _unverifiable_result(exc, app_name)
            block_error = _build_block_error(unverifiable, app_name)
            _emit_outcome(
                "blocked" if enforce else "would_block",
                block_error.details[0].code,
                unverifiable.checks,
                CLASSIFICATION_SOURCE_UNVERIFIABLE,
                audience=block_error.details[0].audience.value,
                # The exception, not exc_info=True: on the budget-overrun path this
                # runs outside any ``except`` (the AppTimeoutError is constructed,
                # not caught), so sys.exc_info() is empty and True would attach
                # nothing — on the one path where the cause is the whole diagnostic.
                exc_info=exc,
            )
            if enforce:
                raise block_error
            return unverifiable

        started = time.monotonic()
        budget = _effective_budget(budget_seconds)
        # Resolve inside the activity (the workflow forwarded only references).
        try:
            credentials, credentials_by_name = await _resolve_gate_credentials(input)
        except Exception as e:
            # Resolution is gate plumbing, so the default here is the opposite of
            # the handler path below: only a *provable* credential absence is a
            # config fact this run can be blamed for. Everything else — including
            # the resolver's collapsed "unexpected vault error" not-founds —
            # propagates and fails open.
            if not _is_definitive_credential_absence(e):
                raise
            return _no_verdict(e)

        remaining = budget - (time.monotonic() - started)
        if remaining < _min_handler_seconds(budget):
            # Resolution ate the budget. That is the secret store being slow, not
            # the source being unready — fail open rather than calling the handler
            # with no time and blaming it for the timeout.
            raise DependencyUnavailableError(
                message=(
                    "Credential resolution consumed the entire preflight budget "
                    f"({budget:.0f}s); no time left to verify the source"
                ),
                service="secret_store",
            )

        # Floor of the remaining budget, and the one number the handler is told —
        # the timeout message quotes it too, so what we enforce, what we report,
        # and what we blame are all the same value. With storage verification
        # opted in, the storage floor is reserved out of the *advertised*
        # number: a handler that sizes its probes to this field — exactly what
        # the docs say to do — must still leave the storage check its floor,
        # or the opt-in silently degrades to a skip.
        reserved = _STORAGE_CHECK_MIN_SECONDS if verify_storage else 0.0
        handler_budget = max(1, int(remaining - reserved))
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
            # What is actually left, not the nominal budget: resolution above has
            # already spent part of it. A handler sizing probes to this number is
            # sizing to the deadline the wait_for below really enforces.
            timeout_seconds=handler_budget,
        )
        # Redact every resolved secret from logs — the single-triple list and
        # every named group, without assuming which path populated which.
        all_creds = [
            *credentials,
            *(c for group in credentials_by_name.values() for c in group),
        ]
        # _no_verdict raises in hard mode, so it must never be called from inside
        # this try — the raise would be re-caught below and _no_verdict would run
        # a second time, double-emitting the outcome row and reclassifying the
        # failure. Hence the flag: the timeout is handled after the try closes.
        timed_out = False
        try:
            with bind_invocation_context(app_name, all_creds):
                # Deliberately not asyncio.wait_for: it cancels the handler and
                # then *awaits* it, so a handler that swallows CancelledError
                # either returns a value (wait_for hands it back and the budget
                # is never enforced) or keeps running past start_to_close (the
                # activity is killed and the classification is lost — the very
                # defect this gate exists to fix). Waiting on the task instead
                # lets us classify *at* the deadline, whatever the handler does.
                check = asyncio.ensure_future(handler.preflight_check(preflight_input))
                done, _ = await asyncio.wait({check}, timeout=remaining)
                if done:
                    result = check.result()
                else:
                    # Ask it to stop, but never await it — an uncooperative
                    # handler must not be able to hold the activity open.
                    check.cancel()
                    # Abandoning a task without awaiting leaves any exception it
                    # later raises unretrieved, which asyncio logs on GC. Consume
                    # it (same fire-and-forget idiom as _runtime/offload.py).
                    check.add_done_callback(
                        lambda f: None if f.cancelled() else f.exception()
                    )
                    timed_out = True
        except Exception as e:
            # A handler that raises the deliberate block itself already carries a
            # verdict and its own emitted row; pass it straight through rather
            # than re-wrapping it as an unverifiable source.
            if _is_gate_broken(e) or is_preflight_block(e):
                raise
            return _no_verdict(e)

        if timed_out:
            return _no_verdict(
                AppTimeoutError(
                    message=(
                        "Preflight checks did not finish within the "
                        f"{handler_budget}s budget"
                    ),
                    app_name=app_name,
                    retryable=False,
                )
            )
        # Per-app opt-in: the handler certified the source; now certify the
        # stores this run will upload artifacts to, in whatever budget is left.
        # Appends checks and may downgrade READY → NOT_READY, so it must run
        # before the verdict evaluation below. Never raises; see its docstring
        # for the fail-open/verdict taxonomy note.
        if verify_storage and await _append_storage_checks(result, budget, started):
            # A failed probe only becomes a verdict once the app's retry
            # attempts are exhausted — one flaky probe must not block a
            # hard-mode run on its first attempt. Same deferral the handler
            # no-verdict path uses; the retried attempt re-runs everything.
            if not _is_final_attempt(attempts):
                from application_sdk.execution.errors import (  # noqa: PLC0415 — avoid import cycle at module load
                    ApplicationError,
                )

                failed_stores = ", ".join(
                    c.name
                    for c in result.checks
                    if not c.passed and c.name.startswith("objectStoreAccess:")
                )
                raise ApplicationError(
                    "Preflight storage checks failed; deferring to the gate's "
                    f"retry policy ({failed_stores})",
                    type=PREFLIGHT_NO_VERDICT_ERROR_TYPE,
                )
        # The outcome event is the gate's queryable row (connector-pulse builds the
        # dashboard from it). The activity holds the verdict, so it emits the
        # proceeded/blocked rows; the workflow emits only no_verdict (fail-open).
        # ``reason`` is the status on proceed, the primary FailureDetails.code on a
        # block. Activity execution is at-least-once, so a retry after a lost
        # completion can re-emit — consumers dedupe on (workflow_run_id, outcome).
        if result.status is PreflightStatus.NOT_READY:
            block_error = _build_block_error(result, app_name)
            _emit_outcome(
                "blocked" if enforce else "would_block",
                block_error.details[0].code,
                result.checks,
                CLASSIFICATION_VERDICT,
                audience=block_error.details[0].audience.value,
            )
            if enforce:
                raise block_error
            # Soft: the verdict stays honest NOT_READY; the gate just does not
            # enforce it. The would_block row above is the loud record.
            return result
        _emit_outcome(
            "proceeded", result.status.value, result.checks, CLASSIFICATION_VERDICT
        )
        return result

    return preflight_gate
