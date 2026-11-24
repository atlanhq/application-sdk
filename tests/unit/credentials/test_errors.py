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


# ---------------------------------------------------------------------------
# One credential family (FND-4298)
# ---------------------------------------------------------------------------


def _client_credential_errors() -> list[Exception]:
    from application_sdk.clients.azure.azure_errors import (
        AzureCredentialError,
        AzureCredentialParseError,
        AzureCredentialTypeError,
    )
    from application_sdk.clients.sql_errors import (
        SqlAwsCredentialsError,
        SqlCredentialsParseError,
    )

    return [
        SqlCredentialsParseError(field="authType"),
        SqlAwsCredentialsError(message="no role"),
        AzureCredentialParseError(message="bad"),
        AzureCredentialTypeError(),
        AzureCredentialError(),
    ]


@pytest.mark.parametrize(
    "err", _client_credential_errors(), ids=lambda e: type(e).__name__
)
def test_except_credential_error_catches_every_client_credential_error(
    err: Exception,
) -> None:
    from application_sdk.credentials.errors import CredentialError

    with pytest.raises(CredentialError):
        raise err


def test_every_credential_parse_failure_has_one_code() -> None:
    """Three classes, two codes one letter apart, and a third, before FND-4298."""
    from application_sdk.clients.azure.azure_errors import AzureCredentialParseError
    from application_sdk.clients.sql_errors import SqlCredentialsParseError
    from application_sdk.credentials.errors import CredentialParseError

    for cls in (SqlCredentialsParseError, AzureCredentialParseError):
        assert issubclass(cls, CredentialParseError)
        assert cls.code == CredentialParseError.code == "INVALID_INPUT_CREDENTIAL_PARSE"


def test_a_client_parse_error_keeps_its_evidence_fields() -> None:
    from application_sdk.clients.azure.azure_errors import AzureCredentialParseError
    from application_sdk.errors.categories import FailureCategory

    err = AzureCredentialParseError(message="bad", received_auth_type="msi")
    details = err.to_failure_details()
    assert details.category is FailureCategory.INVALID_INPUT
    assert details.code == "INVALID_INPUT_CREDENTIAL_PARSE"
    assert details.evidence["received_auth_type"] == "msi"
    assert details.evidence["field"] == "credentials"


@pytest.mark.parametrize(
    "err", _client_credential_errors(), ids=lambda e: type(e).__name__
)
def test_a_client_credential_error_renders_like_its_family(err: Exception) -> None:
    """The dataclass-generated __init__ skips CredentialError.__init__; nothing
    it sets is lost. ``str()`` takes the family's AAF prefix from the MRO, and
    the deprecated per-instance override stays unset."""
    from application_sdk.errors._legacy_codes import legacy_code_for

    code = legacy_code_for(type(err))
    assert code is not None
    assert str(err).startswith(f"[{code.code}] ")
    assert err._error_code is None  # type: ignore[attr-defined]
    assert err.credential_name is None  # type: ignore[attr-defined]
