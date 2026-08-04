"""Tests for the SDR workflow → frontend response envelopes."""

from __future__ import annotations

from application_sdk.handler.contracts import (
    AuthOutput,
    AuthStatus,
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.handler.sdr_output import (
    auth_output_to_response,
    preflight_output_to_response,
)


def test_preflight_output_to_response_envelope() -> None:
    out = PreflightOutput(
        status=PreflightStatus.NOT_READY,
        message="blocked",
        checks=[
            PreflightCheck(name="Connectivity check", passed=True, message="ok"),
            PreflightCheck(name="Secret store", passed=False, message="down"),
        ],
    )
    resp = preflight_output_to_response(out)
    # envelope.success = "any check ran" (so SageV2 renders per-check rows)
    assert resp["success"] is True
    # camelCase check keys with success/successMessage/failureMessage
    assert resp["data"]["connectivity check"]["success"] is True
    assert resp["data"]["connectivity check"]["successMessage"] == "ok"
    assert resp["data"]["secret store"]["success"] is False
    assert resp["data"]["secret store"]["failureMessage"] == "down"
    # canonical verdict under preflight
    assert resp["preflight"]["status"] == "not_ready"
    assert len(resp["preflight"]["checks"]) == 2


def test_preflight_no_checks_success_false() -> None:
    out = PreflightOutput(status=PreflightStatus.NOT_READY, message="x", checks=[])
    assert preflight_output_to_response(out)["success"] is False


def test_auth_output_to_response_envelope() -> None:
    resp = auth_output_to_response(
        AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")
    )
    assert resp["success"] is True
    assert resp["message"] == "auth ok"
    assert resp["data"]["status"] == "success"


def test_metadata_output_to_response_is_bare_list() -> None:
    from application_sdk.handler.contracts import SqlMetadataOutput
    from application_sdk.handler.sdr_output import metadata_output_to_response

    out = SqlMetadataOutput(objects=[{"TABLE_CAT": "db1"}, {"TABLE_CAT": "db2"}])
    resp = metadata_output_to_response(out)
    assert isinstance(resp, list)
    assert resp == [{"TABLE_CAT": "db1"}, {"TABLE_CAT": "db2"}]
