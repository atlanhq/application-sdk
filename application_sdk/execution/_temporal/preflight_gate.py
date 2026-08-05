"""Injected pre-extraction preflight gate (HYP-1883).

A core SDK extraction-lifecycle activity. Every generated workflow's ``_run``
dispatches ``{app}:preflight`` as its first step (see
:func:`application_sdk.app.base._run_preflight_gate`). ``READY`` and ``PARTIAL``
return normally.

The activity raises ``ApplicationError(type="PreflightFailed")`` — red in Temporal,
attributed to preflight — for anything attributable to the **source** while the app
is in hard mode: a ``NOT_READY`` verdict, a probe overrunning the budget, a handler
crash, or a provably absent credential. Failures of our own **plumbing** propagate
instead, so the workflow fails open in either mode; a platform blip must never fail
a healthy run. The line is drawn off the raised error's own ``FailureCategory``
(CNCT-99) — see :func:`application_sdk.checks.runner.is_plumbing_failure`.

**What lives here, after consolidation:** posture. Running the checks — credential
resolution, the budget enforced net of it, the timeout that classifies *at* the
deadline, the verdict, the outcome row — is :mod:`application_sdk.checks`, shared
with the config UI, the SDR connectivity test, and scheduled drift detection so all
four ask the handler the same question. This module decides only what to *do* with a
blocking verdict, which is a decision no other caller has.

Enforcement stays in the activity rather than the workflow because this is the only
frame holding the resolved posture, and because the activity is where the handler is
bounded — if Temporal's ``start_to_close`` killed it first there would be no frame
left to classify the failure in. Credential resolution likewise happens *inside* the
activity; the deterministic workflow only forwards the secret-free
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
    from application_sdk.checks.credentials import CredentialSource
    from application_sdk.checks.outcome import (
        EMPTY_CHECK_MATRIX,
        PREFLIGHT_FALLBACK_CODE,
        PREFLIGHT_OUTCOME_EVENT,
        PREFLIGHT_POSTURE_EVENT,
    )
    from application_sdk.checks.outcome import emit as emit_outcome
    from application_sdk.checks.outcome import log_posture, outcome_for
    from application_sdk.checks.projections import (
        PREFLIGHT_FAILED_ERROR_TYPE,
        PREFLIGHT_NO_VERDICT_ERROR_TYPE,
        is_block,
        to_block_error,
    )
    from application_sdk.checks.request import (
        CheckRequest,
        CheckTrigger,
        config_from_snapshot,
    )
    from application_sdk.checks.runner import UNVERIFIABLE_CHECK_NAME, run_checks
    from application_sdk.checks.verdict import CheckClassification, CheckVerdict
    from application_sdk.credentials.ref import CredentialRef, CredentialResolvable
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.handler.contracts import PreflightOutput, PreflightStatus
    from application_sdk.observability.logger_adaptor import (
        CHECK_MATRIX_KEY,
        get_logger,
    )

logger = get_logger(__name__)

__all__ = [
    # Enforcement — what this module still owns.
    "build_preflight_gate_activity",
    "gate_retry_policy",
    "gate_timeouts",
    "input_type_supports_gate",
    "is_preflight_block",
    "log_gate_posture",
    "preflight_gate_activity_name",
    "resolve_gate_attempts",
    "resolve_gate_budget_seconds",
    "PreflightGateInput",
    # Budget/attempt bounds, declared per app.
    "GATE_ACTIVITY_HEADROOM_SECONDS",
    "GATE_ATTEMPTS_DEFAULT",
    "GATE_ATTEMPTS_MAX",
    "GATE_ATTEMPTS_MIN",
    "GATE_RETRY",
    "GATE_TIMEOUT_DEFAULT_SECONDS",
    "GATE_TIMEOUT_MAX_SECONDS",
    "GATE_TIMEOUT_MIN_SECONDS",
    # Contract strings and classifications, re-exported from the check core so
    # existing importers keep working — the pinned thing is the value, not its home.
    # CHECK_MATRIX_KEY rides along because the workflow's own fail-open emission
    # (``app/base.py``) takes its whole attribute vocabulary from this module.
    "CHECK_MATRIX_KEY",
    "CLASSIFICATION_GATE_BROKEN",
    "CLASSIFICATION_SOURCE_UNVERIFIABLE",
    "CLASSIFICATION_VERDICT",
    "EMPTY_CHECK_MATRIX",
    "PREFLIGHT_FAILED_ERROR_TYPE",
    "PREFLIGHT_FALLBACK_CODE",
    "PREFLIGHT_NO_VERDICT_ERROR_TYPE",
    "PREFLIGHT_OUTCOME_EVENT",
    "PREFLIGHT_POSTURE_EVENT",
    "UNVERIFIABLE_CHECK_NAME",
]

# The observability and error-shape contract now lives with the shared check core
# (``application_sdk.checks``), so every path emits identical rows and builds
# identical block errors. The names stay importable from here — the workflow in
# ``app/base.py``, the log interceptor, and the gate's tests all ask this module for
# them, and what is pinned is the *string*, not its address. See ``__all__`` below.


def is_preflight_block(exc: BaseException | None) -> bool:
    """Whether ``exc`` (or any cause in its chain) is the deliberate gate block.

    Alias for :func:`application_sdk.checks.projections.is_block`. Kept because
    this is the name the workflow and the log interceptor ask for when they need to
    tell a deliberate abort apart from a real failure.
    """
    return is_block(exc)


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

# Why a run could not reach a verdict. Stamped on the outcome event so
# connector-pulse can separate "we know we couldn't ask the source" from "our own
# plumbing broke" — only the former is subject to gate mode. The values live on
# ``CheckClassification`` now; these aliases keep the workflow's fail-open emission
# in ``app/base.py`` (and its tests) importing from where they always did.
CLASSIFICATION_SOURCE_UNVERIFIABLE = CheckClassification.SOURCE_UNVERIFIABLE.value
CLASSIFICATION_GATE_BROKEN = CheckClassification.GATE_BROKEN.value
CLASSIFICATION_VERDICT = CheckClassification.VERDICT.value


def log_gate_posture(app_name: str, *, enforce: bool, budget_seconds: int) -> None:
    """Emit the queryable boot-time posture row for one gate-registered app.

    Alias for :func:`application_sdk.checks.outcome.log_posture`. Posture is a
    gate-only concept, so this is the name the worker calls; the emission itself
    lives with the other pinned contract rows so their attribute sets cannot drift.
    """
    log_posture(app_name, enforce=enforce, budget_seconds=budget_seconds)


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


def _config_from_snapshot(
    snapshot: dict[str, Any], drop_keys: Iterable[str] = ()
) -> dict[str, Any]:
    """Extract preflight form config from a raw extraction-input snapshot.

    Alias for :func:`application_sdk.checks.request.config_from_snapshot`, which
    holds the implementation (including why hyphenated aliases are emitted and why
    ``False``/``0`` survive the empty-value filter). Kept as a module-level name:
    this is the gate's input assembly, and it reads better beside the activity that
    calls it.
    """
    return config_from_snapshot(snapshot, drop_keys)


def _enforced_deadline_seconds() -> float | None:
    """The deadline Temporal will actually enforce on this activity, less headroom.

    The worker builds the activity with the app's budget while the workflow sizes
    ``start_to_close`` from the same ``ClassVar`` — two independent reads that can
    skew during a rolling deploy, or when the worker cannot resolve the app class and
    falls back to the default. Handing the real deadline to the runner lets it cap
    its own budget, which is what makes "our timeout wins the race" true by
    construction; if Temporal won, the activity would be killed before it could
    classify the failure or consult the posture (the CNCT-99 defect).

    ``None`` when there is no activity context (a direct call or a unit test) or the
    deadline cannot be read — the runner then simply honours the declared budget.
    """
    try:
        start_to_close = activity.info().start_to_close_timeout
    except RuntimeError:
        # No activity context (direct call, unit test) — expected, not notable.
        return None
    # This exists to stop a budget skew from breaking a run, so it must never become
    # the thing that breaks one; anything unexpected degrades to the declared budget.
    except Exception:
        logger.debug(
            "Could not read the activity's start_to_close; using the declared "
            "preflight budget",
            exc_info=True,
        )
        return None
    if not isinstance(start_to_close, timedelta):
        return None
    ceiling = start_to_close.total_seconds() - GATE_ACTIVITY_HEADROOM_SECONDS
    return ceiling if ceiling > 0 else None


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


def _credential_source(input: PreflightGateInput) -> CredentialSource:
    """Map the gate's secret-free envelope onto the shared credential source.

    The gate never carries inline credentials — it is handed references only, by
    construction (the deterministic workflow must not touch secrets) — so
    ``inline`` stays empty and resolution always dereferences.
    """
    return CredentialSource(
        extraction_method=input.extraction_method,
        credential_guid=input.credential_guid,
        agent_json=input.agent_json,
        credential_ref=input.credential_ref,
        named_ref_fields=input.credential_ref_fields,
        field_values=input.extraction_snapshot,
    )


def build_preflight_gate_activity(
    handler: Handler,
    app_name: str,
    *,
    enforce: bool = False,
    budget_seconds: float = GATE_TIMEOUT_DEFAULT_SECONDS,
    attempts: int = GATE_ATTEMPTS_DEFAULT,
) -> Callable[..., Awaitable[Any]]:
    """Build the injected preflight-gate activity (``{app}:preflight``).

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
        gate_mode = "hard" if enforce else "soft"

        def _block(verdict: CheckVerdict) -> PreflightOutput:
            """Apply this app's posture to a blocking verdict.

            The gate's whole remaining job. Hard mode raises and aborts the run;
            soft mode returns the honest ``NOT_READY`` and lets the run proceed,
            with the dodged block recorded as ``would_block`` so adoption stays
            measurable before enforcement is switched on anywhere.
            """
            block_error = to_block_error(verdict)
            emit_outcome(
                verdict,
                outcome=outcome_for(blocked=True, enforce=enforce),
                reason=block_error.details[0].code,
                gate_mode=gate_mode,
            )
            if enforce:
                raise block_error
            return verdict.output

        # Build the request in the activity frame: reading app-authored fields off
        # the snapshot must not happen in the deterministic workflow, which is why
        # the workflow forwards the raw snapshot rather than the derived config.
        snapshot_config = _config_from_snapshot(
            input.extraction_snapshot, input.credential_ref_fields.values()
        )
        request = CheckRequest(
            app_name=app_name,
            entrypoint=input.entrypoint,
            trigger=CheckTrigger.PRE_RUN,
            credential_source=_credential_source(input),
            # One snapshot-derived view of the form, so both config fields carry it —
            # the gate has no separate metadata to distinguish, and handlers in the
            # fleet read either field.
            metadata_config=snapshot_config,
            connection_config=snapshot_config,
            budget_seconds=budget_seconds,
        )
        # Plumbing failures propagate out of run_checks, which is the workflow's
        # mode-blind fail-open: a platform blip must never fail a healthy run.
        verdict = await run_checks(
            handler,
            request,
            enforced_deadline_seconds=_enforced_deadline_seconds(),
            attempt=_current_attempt(),
            # The gate already folds the object-store probe in via the core; the
            # default is fine here and stated explicitly because this is the path
            # that historically lacked it.
            augment_object_store=True,
        )

        if verdict.classification is CheckClassification.SOURCE_UNVERIFIABLE:
            if not _is_final_attempt(attempts):
                # Let the app's retry policy have its turn; a slow source may answer
                # next attempt. Deliberately not the block error type, so the
                # workflow does not abort on a first slow attempt.
                from application_sdk.execution.errors import (  # noqa: PLC0415 — avoid import cycle at module load
                    ApplicationError,
                )

                raise ApplicationError(
                    f"Preflight could not reach a verdict: {verdict.output.message}",
                    type=PREFLIGHT_NO_VERDICT_ERROR_TYPE,
                )
            logger.error(
                "Preflight gate could not verify the source (gate_mode=%s): %s",
                gate_mode,
                "blocking the run before extraction"
                if enforce
                else "proceeding without source verification",
            )
            return _block(verdict)

        if verdict.status is PreflightStatus.NOT_READY:
            return _block(verdict)

        emit_outcome(
            verdict,
            outcome=outcome_for(blocked=False, enforce=enforce),
            reason=verdict.status.value,
            gate_mode=gate_mode,
        )
        return verdict.output

    return preflight_gate
