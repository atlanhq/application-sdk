"""Tests for credential error string sanitization.

CredentialError.__str__ flows into HTTP error responses via
``detail=str(e)`` — causes embedding connection strings or secret values
must be redacted.
"""

import pytest

from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
    CredentialValidationError,
)
from application_sdk.errors import CREDENTIAL_ERROR, CREDENTIAL_NOT_FOUND
from application_sdk.errors.leaves import AuthError, InvalidInputError


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


@pytest.mark.parametrize(
    ("cls", "leaf"),
    [
        (CredentialError, AuthError),
        (CredentialParseError, InvalidInputError),
        (CredentialValidationError, InvalidInputError),
    ],
)
def test_shared_init_runs_the_class_own_leaf(cls: type, leaf: type) -> None:
    """The subclasses reuse CredentialError.__init__ via ``_CATEGORICAL_BASE`` (FND-4917)."""
    assert cls._CATEGORICAL_BASE is leaf
    err = cls("positional message", credential_name="pg", error_code=CREDENTIAL_ERROR)
    assert err.category is leaf.category
    assert err.error_code is CREDENTIAL_ERROR
    assert str(err) == f"[{CREDENTIAL_ERROR.code}] positional message | credential=pg"
    assert err.to_failure_details().evidence["credential_name"] == "pg"


def test_not_found_error_code_and_str() -> None:
    err = CredentialNotFoundError("guid-1")
    assert err.error_code is CREDENTIAL_NOT_FOUND
    assert str(err) == "[AAF-CRD-002] Credential 'guid-1' not found | credential=guid-1"
