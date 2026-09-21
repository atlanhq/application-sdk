"""Nothing bound for the HTTP wire may carry a credential.

The hosted path and the worker path used to disagree about this: application_sdk
redacted, server_sdk did not, and server_sdk is the side facing Atlan.
``_summarize_check`` serialises the whole ``PreflightCheck`` -- evidence dict
included -- into the ``/workflows/v1/check`` response body, so a driver
exception carrying a DSN shipped the source password to the caller.

Also pins the preflight message-resolution precedence, which had no executed
test at all despite being the rule the typed-error envelope exists to serve.
"""

from __future__ import annotations

import json

import pytest
from server_sdk.errors.leaves import AuthError, SourceUnavailableError
from server_sdk.errors.redaction import (
    mask_secret_named_keys,
    redact_secrets,
    redact_wire_value,
    sanitize_cause_repr,
    secret_named_evidence_keys,
)
from server_sdk.errors.wire import FailureDetails
from server_sdk.handler.contracts import PreflightCheck
from server_sdk.server import _summarize_check

DSN = "postgresql://atlanadmin:sup3rs3cr3t@warehouse.internal:5439/db"
SECRETS = ("sup3rs3cr3t", "hunter2", "AKIA-LIVE-KEY")


def _wire_blob(obj) -> str:
    # ensure_ascii=False so the truncation marker stays comparable as itself.
    return json.dumps(obj, default=str, ensure_ascii=False)


# ── string redaction ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (DSN, "postgresql://***@warehouse.internal:5439/db"),
        # A raw @ inside the password must not leave the tail exposed.
        ("postgresql://u:p@ss@host:5432/db", "postgresql://***@host:5432/db"),
        # ODBC quotes values containing the ';' separator.
        ("UID=sa;PWD={s3cr;et};Server=x", "UID=sa;PWD=***;Server=x"),
        # Presigned object-store URL: the signature authorises the request.
        ("https://x.blob.core.windows.net/c?sig=AB%2Fd&se=2026",
         "https://x.blob.core.windows.net/c?sig=***&se=2026"),
    ],
)
def test_redact_secrets(raw: str, expected: str) -> None:
    assert redact_secrets(raw) == expected


def test_operator_identifiers_survive() -> None:
    """Over-redaction costs on-call the correlation IDs they triage with."""
    keep = "run_guid=7f3a correlation_uuid=99b1 next_token=page-42 uid=svc_reader"
    assert redact_secrets(keep) == keep


# ── evidence keys ───────────────────────────────────────────────────────────


def test_secret_named_keys_are_masked_not_dropped() -> None:
    ev = {"db_password": "hunter2", "api_key": "AKIA-LIVE-KEY", "host": "wh.internal"}
    masked = mask_secret_named_keys(ev)
    assert masked["db_password"] == "***"
    assert masked["api_key"] == "***"
    # The key survives so the envelope still says a credential was involved.
    assert set(masked) == set(ev)
    assert masked["host"] == "wh.internal"


def test_generic_key_names_are_not_swept_up() -> None:
    assert secret_named_evidence_keys({"object_key": "k", "cache_key": "c"}) == frozenset()


def test_masking_never_raises() -> None:
    """server_sdk masks where application_sdk rejects.

    ``handler/sql.py`` builds failure details inside a "report NOT_READY, never
    500" boundary, so a raising validator would turn a redaction problem into
    exactly the 500 that boundary exists to prevent.
    """
    fd = FailureDetails(
        category=AuthError.category,
        code="AUTH",
        retryable=False,
        message="denied",
        evidence={"password": "hunter2"},
    )
    assert fd.evidence["password"] == "***"


# ── hostile structures degrade, never hang ──────────────────────────────────


def test_self_referential_evidence_is_pruned_not_recursed() -> None:
    ev: dict = {"password": "hunter2"}
    ev["self"] = ev
    assert redact_wire_value(ev)["self"] is None


def test_depth_is_bounded() -> None:
    deep: dict = {}
    node = deep
    for _ in range(80):
        node["n"] = {}
        node = node["n"]
    node["dsn"] = DSN
    blob = _wire_blob(redact_wire_value(deep))
    assert "…" in blob
    # Truncation must drop the secret, not merely stop walking above it.
    assert "sup3rs3cr3t" not in blob


# ── the full envelope ───────────────────────────────────────────────────────


def test_no_secret_reaches_the_failure_envelope() -> None:
    err = SourceUnavailableError(
        f"could not connect: {DSN}",
        dsn=DSN,
        db_password="hunter2",
        api_key="AKIA-LIVE-KEY",
        host="warehouse.internal",
    )
    blob = _wire_blob(err.to_failure_details().model_dump(mode="json"))
    assert not [s for s in SECRETS if s in blob], blob
    # Redaction must not cost the diagnostic.
    assert "warehouse.internal" in blob


def test_preflight_check_message_is_scrubbed() -> None:
    """`message=str(exc)` is the documented SQL-connector fallback, and it does
    not go through FailureDetails."""
    check = PreflightCheck(name="connectivity", passed=False, message=f"boom {DSN}")
    assert "sup3rs3cr3t" not in check.message


def test_cause_repr_never_reaches_the_http_caller() -> None:
    err = SourceUnavailableError("warehouse is resuming")
    fd = err.to_failure_details().model_copy(
        update={"cause_repr": "OperationalError: " + DSN}
    )
    summary = _summarize_check(
        PreflightCheck(name="connectivity", passed=False, error=fd)
    )
    assert "cause_repr" not in summary.get("error", {})
    assert "sup3rs3cr3t" not in _wire_blob(summary)


def test_sanitize_cause_repr_redacts_before_truncating() -> None:
    exc = RuntimeError(DSN + " x" * 4000)
    out = sanitize_cause_repr(exc)
    assert "sup3rs3cr3t" not in out
    assert out.startswith("RuntimeError: ")
    assert "elided" in out


# ── message-resolution precedence (previously unexecuted) ───────────────────


def test_failed_check_error_message_beats_its_plain_message() -> None:
    check = PreflightCheck(
        name="connectivity",
        passed=False,
        message="generic failure",
        error=SourceUnavailableError(
            "warehouse is resuming", suggested_action="retry in 60s"
        ),
    )
    assert check.resolved_message == "warehouse is resuming"
    assert check.resolved_suggested_action == "retry in 60s"
    summary = _summarize_check(check)
    assert summary["message"] == "warehouse is resuming"
    assert summary["suggested_action"] == "retry in 60s"


def test_a_passing_check_keeps_its_own_message() -> None:
    check = PreflightCheck(name="connectivity", passed=True, message="all good")
    assert check.resolved_message == "all good"
    assert check.resolved_suggested_action == ""


def test_an_app_error_is_coerced_into_failure_details() -> None:
    check = PreflightCheck(name="auth", passed=False, error=AuthError("denied"))
    assert isinstance(check.error, FailureDetails)
    assert check.error.category is AuthError.category
