"""The one place a check outcome is recorded.

These log bodies and attribute names are a **queried contract**, not prose:
connector-pulse builds its dashboards by pattern-matching them in ClickHouse. They
are pinned here as constants so the strings cannot drift between emission sites,
and they must not be reworded.

Before consolidation only the pre-run gate emitted anything at all, so the config
UI and the SDR connectivity test were invisible: there was no way to ask whether an
app's UI was green while its runs were red, which is exactly the app whose broken
guarantee most needs finding. Every path now emits through :func:`emit`, and
:attr:`~application_sdk.checks.request.CheckTrigger` says which path it was.

Compatibility rule for this module: ``pre_run`` rows keep their exact historical
attribute set and values. New information arrives as *new* attributes (``trigger``)
or as new values in fields the older rows do not carry, never by changing what an
existing row means.
"""

from __future__ import annotations

import math
from typing import Any

import orjson

from application_sdk.checks.request import CheckTrigger
from application_sdk.checks.verdict import CheckVerdict
from application_sdk.handler.contracts import PreflightCheck
from application_sdk.observability.logger_adaptor import (
    CHECK_MATRIX_KEY,
    GATE_ATTEMPTS_KEY,
    GATE_CLASSIFICATION_KEY,
    GATE_DURATION_KEY,
    GATE_MODE_KEY,
    GATE_TIMEOUT_KEY,
    get_logger,
)

logger = get_logger(__name__)

# Stable log body for the outcome event — the contract connector-pulse queries on.
PREFLIGHT_OUTCOME_EVENT = "Preflight gate outcome"

# Stable log body for the boot-time posture event, emitted once per gate-registered
# app at worker build. This is the *denominator* the outcome events cannot supply:
# an app that never reaches a verdict emits no outcome row carrying ``gate_mode``,
# so "which apps believe they are gated" is unanswerable from outcomes alone —
# which is exactly the app whose broken guarantee we most need to find.
PREFLIGHT_POSTURE_EVENT = "Preflight gate posture"

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

# ``gate_mode`` for a run nothing can block. Only the pre-run gate has a posture;
# the config UI, the SDR test and a scheduled probe report and move on. A distinct
# value (rather than omitting the field, or borrowing "soft") keeps the attribute
# present on every row while leaving an existing ``gate_mode='hard'`` filter exactly
# as selective as it was — non-enforcing rows fall outside it instead of being
# mis-binned into it.
GATE_MODE_NOT_APPLICABLE = "none"

# Outcome values. "proceeded" and "blocked" mean what they say; "would_block" is
# the soft-mode record of a block that was dodged, which is what makes adoption
# measurable before enforcement is turned on anywhere.
OUTCOME_PROCEEDED = "proceeded"
OUTCOME_BLOCKED = "blocked"
OUTCOME_WOULD_BLOCK = "would_block"


def check_matrix_json(checks: list[PreflightCheck]) -> str:
    """Compact per-check matrix for the outcome event, as one JSON string.

    Lands as a single ``LogAttributes`` value in ClickHouse, so connector-pulse can
    pattern-match verdicts against workflow outcomes (``JSONExtract``) with no
    schema change. Small fixed fields only — messages and evidence stay in the
    activity result, which is where a human looks.

    Blocking intent is deliberately not a per-check field: it is observable from
    the outcome itself (``would_block``/``blocked`` means the aggregate was
    ``NOT_READY``; a failed check on a ``proceeded`` run is advisory by the
    handler's own choice).
    """
    rows: list[dict[str, Any]] = []
    for check in checks:
        rows.append(
            {
                "name": check.name,
                "passed": check.passed,
                "error_code": check.error.code if check.error else "",
                # orjson emits null for nan/inf; normalize to 0.0 so the ClickHouse
                # row stays numeric for downstream JSONExtract, and never raise — a
                # raise here would lose the whole event.
                "duration_ms": check.duration_ms
                if math.isfinite(check.duration_ms)
                else 0.0,
            }
        )
    return orjson.dumps(rows).decode()


def emit(
    verdict: CheckVerdict,
    *,
    outcome: str,
    reason: str,
    gate_mode: str = GATE_MODE_NOT_APPLICABLE,
) -> None:
    """Emit the one queryable row for a completed check run.

    A single site for every outcome so the attribute set cannot drift between them
    — a consumer that finds ``gate_duration_ms`` on ``proceeded`` but not on
    ``would_block`` cannot compute headroom for the runs that need it most.

    ``reason`` is the verdict status on a proceed and the primary
    ``FailureDetails.code`` on a block. ``gate_mode`` is the resolved posture on the
    pre-run path and :data:`GATE_MODE_NOT_APPLICABLE` everywhere else.

    Activity execution is at-least-once, so a retry after a lost completion can
    re-emit; consumers dedupe on ``(workflow_run_id, outcome)``.
    """
    # conformance: ignore[L018] these kwargs ARE the contract — they land as ClickHouse LogAttributes that connector-pulse queries by name; folding them into a %-style message body would make the outcome row unqueryable, which is the entire purpose of the event.
    logger.info(
        PREFLIGHT_OUTCOME_EVENT,
        outcome=outcome,
        reason=reason,
        app_name=verdict.app_name,
        entrypoint=verdict.entrypoint or "<implicit>",
        checks=len(verdict.checks),
        trigger=verdict.trigger.value,
        **{
            CHECK_MATRIX_KEY: check_matrix_json(verdict.checks),
            GATE_MODE_KEY: gate_mode,
            GATE_CLASSIFICATION_KEY: verdict.classification.value,
            GATE_DURATION_KEY: round(verdict.duration_ms, 1),
            GATE_TIMEOUT_KEY: verdict.budget_seconds,
            GATE_ATTEMPTS_KEY: verdict.attempt,
        },
    )


def emit_no_check_row(
    *,
    app_name: str,
    entrypoint: str,
    outcome: str,
    reason: str,
    trigger: CheckTrigger,
    classification: str,
    gate_mode: str = GATE_MODE_NOT_APPLICABLE,
    budget_seconds: int = 0,
    attempt: int = 1,
) -> None:
    """Emit an outcome row for a run that produced no checks at all.

    The fail-open case: the caller never saw a verdict, so there is no
    :class:`CheckVerdict` to report — but the row must still exist, with an empty
    ``check_matrix`` rather than an absent one, or a run that never reached a
    verdict silently vanishes from the denominator it belongs in.
    """
    # conformance: ignore[L018] these kwargs ARE the contract — they land as ClickHouse LogAttributes that connector-pulse queries by name; folding them into a %-style message body would make the outcome row unqueryable, which is the entire purpose of the event.
    logger.info(
        PREFLIGHT_OUTCOME_EVENT,
        outcome=outcome,
        reason=reason,
        app_name=app_name,
        entrypoint=entrypoint or "<implicit>",
        checks=0,
        trigger=trigger.value,
        **{
            CHECK_MATRIX_KEY: EMPTY_CHECK_MATRIX,
            GATE_MODE_KEY: gate_mode,
            GATE_CLASSIFICATION_KEY: classification,
            GATE_TIMEOUT_KEY: budget_seconds,
            GATE_ATTEMPTS_KEY: attempt,
        },
    )


def log_posture(app_name: str, *, enforce: bool, budget_seconds: int) -> None:
    """Emit the queryable boot-time posture row for one gate-registered app.

    Emitted for **every** gate app, soft included — the point is a complete
    denominator. Ranking hard-mode apps that never produce a verdict needs the set
    of apps declaring hard mode, and soft rows are what make adoption and posture
    drift measurable rather than a code-search artifact.

    Separate from the human-facing hard-mode boot warning by design: this body is a
    pinned contract string that must never be reworded, that one is prose an
    operator reads.
    """
    # conformance: ignore[L018] these kwargs ARE the contract — they land as ClickHouse LogAttributes that connector-pulse queries by name; folding them into a %-style message body would make the outcome row unqueryable, which is the entire purpose of the event.
    logger.info(
        PREFLIGHT_POSTURE_EVENT,
        app_name=app_name,
        **{
            GATE_MODE_KEY: "hard" if enforce else "soft",
            GATE_TIMEOUT_KEY: budget_seconds,
        },
    )


def outcome_for(*, blocked: bool, enforce: bool) -> str:
    """The outcome value for a verdict, given posture.

    ``blocked`` is whether the verdict was ``NOT_READY``; ``enforce`` is whether
    anything will act on it. A soft gate reports ``would_block`` — the loud record
    of a block that was dodged.
    """
    if not blocked:
        return OUTCOME_PROCEEDED
    return OUTCOME_BLOCKED if enforce else OUTCOME_WOULD_BLOCK


def dump_check(check: PreflightCheck) -> dict[str, Any]:
    """Serialize one check for an error payload, resolving the message precedence."""
    dumped = check.model_dump(mode="json", exclude_none=True)
    dumped["message"] = check.resolved_message
    return dumped
