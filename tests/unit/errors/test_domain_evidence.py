"""Domain umbrella errors expose their context fields in wire evidence."""

import pytest

from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
    CredentialValidationError,
)
from application_sdk.infrastructure.secrets import SecretNotFoundError, SecretStoreError
from application_sdk.storage.errors import (
    StorageConfigError,
    StorageError,
    StorageNotFoundError,
    StoragePermissionError,
)


def test_credential_error_evidence_includes_credential_name() -> None:
    e = CredentialError("auth failed", credential_name="my-cred")
    assert e.to_failure_details().evidence["credential_name"] == "my-cred"


def test_credential_not_found_evidence_includes_credential_name() -> None:
    e = CredentialNotFoundError("my-cred-guid")
    assert e.to_failure_details().evidence["credential_name"] == "my-cred-guid"


def test_credential_parse_error_evidence_includes_credential_name() -> None:
    e = CredentialParseError("bad payload", credential_name="svc-account")
    assert e.to_failure_details().evidence["credential_name"] == "svc-account"


def test_credential_validation_error_evidence_includes_credential_name() -> None:
    e = CredentialValidationError("schema mismatch", credential_name="oauth-cred")
    assert e.to_failure_details().evidence["credential_name"] == "oauth-cred"


def test_storage_error_evidence_includes_key() -> None:
    e = StorageError("unavailable", key="artifacts/run/output.parquet")
    assert e.to_failure_details().evidence["key"] == "artifacts/run/output.parquet"


def test_storage_not_found_evidence_includes_key() -> None:
    e = StorageNotFoundError("not found", key="artifacts/run/output.parquet")
    assert e.to_failure_details().evidence["key"] == "artifacts/run/output.parquet"


def test_storage_permission_error_evidence_includes_key() -> None:
    e = StoragePermissionError("access denied", key="artifacts/run/output.parquet")
    assert e.to_failure_details().evidence["key"] == "artifacts/run/output.parquet"


def test_storage_config_error_evidence_includes_key() -> None:
    e = StorageConfigError("missing bucket", key="artifacts/run/output.parquet")
    assert e.to_failure_details().evidence["key"] == "artifacts/run/output.parquet"


def test_secret_store_error_evidence_includes_secret_name() -> None:
    e = SecretStoreError("store unavailable", secret_name="DB_CONN")
    assert e.to_failure_details().evidence["secret_name"] == "DB_CONN"


def test_secret_not_found_evidence_includes_secret_name() -> None:
    e = SecretNotFoundError("DB_PASSWORD")
    assert e.to_failure_details().evidence["secret_name"] == "DB_PASSWORD"


@pytest.mark.parametrize(
    "evidence_key",
    ["credential_name", "key", "secret_name"],
)
def test_domain_fields_not_in_secret_denylist(evidence_key: str) -> None:
    """References (not secrets themselves) must be allowed in evidence."""
    from application_sdk.errors.wire import _EVIDENCE_KEY_DENYLIST

    assert evidence_key not in _EVIDENCE_KEY_DENYLIST
    assert not any(evidence_key.endswith(s) for s in ("_secret", "_password", "_token"))
