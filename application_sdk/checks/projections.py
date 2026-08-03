"""Rendering a verdict for a particular consumer.

Kept apart from the runner on purpose. A verdict is one thing; the four shapes it
has to appear in are another, and mixing them is how the HTTP route ended up owning
both the widget's field names and its own copy of the error-building logic.

Each function here is a pure projection of a
:class:`~application_sdk.checks.verdict.CheckVerdict`. None of them decide anything.

The Sage widget projection in particular is a **frozen output contract** — see
:func:`to_v2_widget_payload`. It has regressed twice by being "cleaned up".
"""

from __future__ import annotations

from typing import Any

from application_sdk.checks.outcome import PREFLIGHT_FALLBACK_CODE, dump_check
from application_sdk.checks.verdict import CheckVerdict
from application_sdk.errors.leaves import PreconditionError
from application_sdk.handler.contracts import PreflightCheck, PreflightOutput

# Error type carried on the deliberate pre-run block. Red in Temporal, attributed
# to preflight, and the marker :func:`is_block` looks for.
PREFLIGHT_FAILED_ERROR_TYPE = "PreflightFailed"

# Error type for a retryable no-verdict on a non-final attempt. Deliberately NOT
# the block type: the workflow must not treat it as the deliberate block and abort
# before the retry has had its turn.
PREFLIGHT_NO_VERDICT_ERROR_TYPE = "PreflightNoVerdict"


def to_v2_widget_payload(checks: list[PreflightCheck]) -> dict[str, Any]:
    """Project checks into the SageV2 widget's ``data`` map.

    **Frozen contract.** The widget renders
    ``checkResult.success ? successMessage : failureMessage`` with no fallback to
    ``message``, so omitting either field leaves the detail panel blank on a failed
    check — the DBBI-665 / WARE-1250 regressions. ``message`` is retained alongside
    them for consumers already reading the v3 field.

    Check names become camelCase keys so the frontend can iterate them directly.
    """
    payload: dict[str, Any] = {}
    for check in checks:
        # "AuthCheck" -> "authCheck"
        key = check.name[0].lower() + check.name[1:]
        msg = check.resolved_message or ""
        payload[key] = {
            "success": check.passed,
            "message": msg,
            "successMessage": msg if check.passed else "",
            "failureMessage": "" if check.passed else msg,
        }
    return payload


def envelope_success(checks: list[PreflightCheck]) -> bool:
    """Whether the HTTP envelope's ``success`` flag should be true.

    Reports whether preflight *executed*, not whether every check passed —
    per-check pass/fail belongs in ``data.<check>.success``. The widget
    short-circuits on ``!response.success`` and skips the per-check render loop
    entirely, so collapsing this to ``status == READY`` made every
    ``PARTIAL``/``NOT_READY`` response surface as "Check failed" with a blank
    details panel (DBBI-665). Tying it to "any check ran" keeps it false when a
    handler produced nothing and lets the widget render rows otherwise.
    """
    return len(checks) > 0


def to_runtime_summary(output: PreflightOutput) -> dict[str, Any]:
    """Runtime metadata kept outside the widget's ``data`` map.

    ``status`` is the canonical verdict (``ready`` / ``not_ready`` / ``partial``),
    exposed separately precisely because :func:`envelope_success` deliberately does
    not carry it. Per-check ``message``/``suggested_action`` follow the precedence
    rule (a typed ``error`` wins).
    """
    return {
        "status": output.status.value,
        "message": output.message,
        "total_duration_ms": output.total_duration_ms,
        "checks": [_summarize_check(check) for check in output.checks],
    }


def _summarize_check(check: PreflightCheck) -> dict[str, Any]:
    dumped = check.model_dump(mode="json", exclude_none=True)
    dumped["message"] = check.resolved_message
    if check.resolved_suggested_action:
        dumped["suggested_action"] = check.resolved_suggested_action
    return dumped


def to_block_error(verdict: CheckVerdict) -> Any:
    """Build the ``PreflightFailed`` error that aborts a run.

    ``details[0]`` is the primary failure's ``FailureDetails``: the first failed
    check's typed ``error`` when present, else the first failed check's message
    wrapped in ``PreconditionError`` and stamped with the
    :data:`~application_sdk.checks.outcome.PREFLIGHT_FALLBACK_CODE` sentinel, so
    the outcome row's ``reason`` distinguishes an un-migrated block from a typed
    one. ``details[1]`` carries every check, because a *failed* activity has no
    result payload and the red pane would otherwise show nothing.
    """
    from application_sdk.execution.errors import (  # noqa: PLC0415 — avoid import cycle at module load
        ApplicationError,
    )

    failed = verdict.failed_checks
    primary = next((c for c in failed if c.error is not None), None)
    if primary is not None and primary.error is not None:
        details = primary.error
        if details.app_name is None:
            details = details.model_copy(update={"app_name": verdict.app_name})
    else:
        fallback = failed[0].resolved_message if failed else ""
        details = (
            PreconditionError(
                message=fallback or "Preflight check failed",
                app_name=verdict.app_name,
                retryable=False,
            )
            .to_failure_details()
            .model_copy(update={"code": PREFLIGHT_FALLBACK_CODE})
        )

    joined = "; ".join(m for m in (c.resolved_message for c in failed) if m)
    reason = (
        verdict.output.message
        or joined
        or "Preflight check failed; aborting before extraction"
    )
    checks_payload = {"checks": [dump_check(c) for c in verdict.checks]}
    return ApplicationError(
        f"Preflight failed: {reason}",
        details,
        checks_payload,
        type=PREFLIGHT_FAILED_ERROR_TYPE,
        non_retryable=True,
    )


def is_block(exc: BaseException | None) -> bool:
    """Whether ``exc`` (or any cause in its chain) is the deliberate block.

    The marker may sit on a cause rather than the top-level error: the activity
    raises ``ApplicationError(type="PreflightFailed")`` and Temporal wraps it in an
    ``ActivityError``.
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
