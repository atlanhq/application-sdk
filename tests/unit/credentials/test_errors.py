"""Tests for credential error string sanitization.

CredentialError.__str__ flows into HTTP error responses via
``detail=str(e)`` — causes embedding connection strings or secret values
must be redacted.
"""

from application_sdk.credentials.errors import (
    CredentialError,
    CredentialParseError,
    CredentialValidationError,
)


def test_credential_error_str_redacts_cause_connection_string() -> None:
    cause = Exception(
        "could not connect: postgresql://svc_user:Sup3rS3cret@db.internal:5432/prod"
    )
    err = CredentialError("resolution failed", credential_name="pg-prod", cause=cause)
    text = str(err)
    assert "Sup3rS3cret" not in text
    assert "postgresql://***@db.internal:5432/prod" in text
    assert "caused_by=Exception" in text


def test_credential_parse_error_str_redacts_secret_params() -> None:
    cause = ValueError("bad payload: password=hunter2&user=x")
    err = CredentialParseError("parse failed", cause=cause)
    text = str(err)
    assert "hunter2" not in text
    assert "password=***" in text


def test_credential_validation_error_str_keeps_message_and_type() -> None:
    cause = RuntimeError("boom")
    err = CredentialValidationError("invalid credential", cause=cause)
    text = str(err)
    assert "invalid credential" in text
    assert "caused_by=RuntimeError: boom" in text
