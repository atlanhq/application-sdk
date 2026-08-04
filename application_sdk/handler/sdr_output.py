"""FE-facing response envelopes for the SDR ``sdr:*`` workflows.

In the heracles-dispatch model, heracles starts an ``sdr:*`` workflow on the
customer task queue and **forwards the workflow result verbatim** to the
frontend. So the workflow (worker side) must return the exact envelope the
frontend already consumes from the direct-mode HTTP endpoints — rather than a
typed ``PreflightOutput``/``AuthOutput``/``MetadataOutput`` that heracles would
otherwise have to re-transform in Go.

These helpers are the single source of truth for that envelope shape, shared by
the HTTP endpoints (``handler/service.py``) and the ``sdr:*`` activities
(``execution/_temporal/sdr.py``).
"""

from __future__ import annotations

from typing import Any

from application_sdk.handler.contracts import (
    AuthOutput,
    MetadataOutput,
    PreflightCheck,
    PreflightOutput,
)


def _summarize_check(check: PreflightCheck) -> dict[str, Any]:
    dumped = check.model_dump(mode="json", exclude_none=True)
    dumped["message"] = check.resolved_message
    if check.resolved_suggested_action:
        dumped["suggested_action"] = check.resolved_suggested_action
    return dumped


def preflight_runtime_summary(result: PreflightOutput) -> dict[str, Any]:
    """Runtime metadata kept outside the SageV2 ``data`` map (status/diagnostics)."""
    return {
        "status": result.status.value,
        "message": result.message,
        "total_duration_ms": result.total_duration_ms,
        "checks": [_summarize_check(check) for check in result.checks],
    }


def preflight_output_to_response(result: PreflightOutput) -> dict[str, Any]:
    """PreflightOutput → the SageV2 frontend envelope.

    Each check becomes a top-level camelCase key under ``data`` with
    ``success``/``message``/``successMessage``/``failureMessage`` (the SageV2
    widget renders ``success ? successMessage : failureMessage`` with no
    fallback). Envelope ``success`` reports whether *any* check ran (so the
    widget renders per-check rows); the gate verdict lives under ``preflight``."""
    v2_data: dict[str, Any] = {}
    for check in result.checks:
        key = check.name[0].lower() + check.name[1:] if check.name else check.name
        msg = check.resolved_message or ""
        v2_data[key] = {
            "success": check.passed,
            "message": msg,
            "successMessage": msg if check.passed else "",
            "failureMessage": "" if check.passed else msg,
        }
    return {
        "success": len(result.checks) > 0,
        "message": result.message or f"Preflight check {result.status.value}",
        "data": v2_data,
        "preflight": preflight_runtime_summary(result),
    }


def auth_output_to_response(result: AuthOutput) -> dict[str, Any]:
    """AuthOutput → the frontend test-authentication envelope."""
    return {
        "success": result.status.is_success,
        "message": result.message or f"Authentication {result.status.value}",
        "data": result.model_dump(mode="json"),
    }


def metadata_output_to_response(result: MetadataOutput) -> list[Any]:
    """MetadataOutput → the bare object list the frontend filter widgets expect.

    heracles wraps a list workflow result as ``{success, results:[...]}`` (the
    same ``results`` shape the direct ``/credentials/query`` path produces), so
    the sqltree/apitree widgets render the include/exclude tree unchanged."""
    return [
        obj.model_dump() if hasattr(obj, "model_dump") else obj
        for obj in result.objects
    ]
