"""Typed error leaves for the integration test runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DataIntegrityError,
    DependencyUnavailableError,
    InvalidInputError,
    PreconditionError,
)


@dataclass(kw_only=True)
class ValidationInputError(InvalidInputError):
    """A pandera / record-count validation check received invalid input."""

    code: ClassVar[str] = "INVALID_INPUT_VALIDATION"


@dataclass(kw_only=True)
class ComparisonInputError(InvalidInputError):
    """Expected-data file for asset comparison has an invalid structure."""

    code: ClassVar[str] = "INVALID_INPUT_COMPARISON"


@dataclass(kw_only=True)
class HttpClientInputError(InvalidInputError):
    """Unsupported API type passed to the integration HTTP client."""

    code: ClassVar[str] = "INVALID_INPUT_HTTP_CLIENT"
    field: str | None = "api_type"


@dataclass(kw_only=True)
class LocalVaultUnavailableError(DependencyUnavailableError):
    """Could not reach the local-vault service during integration testing."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_LOCAL_VAULT"
    service: str | None = "local-vault"


@dataclass(kw_only=True)
class LocalVaultResponseInvariantError(DataIntegrityError):
    """Local-vault returned 2xx but the expected credential_guid field was absent."""

    code: ClassVar[str] = "DATA_INTEGRITY_LOCAL_VAULT_RESPONSE"
    location: str | None = "local-vault"


@dataclass(kw_only=True)
class IntegrationEnvOrderingError(PreconditionError):
    """An ``ATLAN_*`` env var was set after application_sdk snapshotted it."""

    code: ClassVar[str] = "PRECONDITION_INTEGRATION_ENV_ORDERING"


@dataclass(kw_only=True)
class AppRegistrationMissingError(PreconditionError):
    """The App under test never reached the registry create_worker snapshots."""

    code: ClassVar[str] = "PRECONDITION_APP_NOT_REGISTERED"


@dataclass(kw_only=True)
class GoldenLayoutError(InvalidInputError):
    """A declared golden-corpus layout is self-inconsistent."""

    code: ClassVar[str] = "INVALID_INPUT_GOLDEN_LAYOUT"


@dataclass(kw_only=True)
class GoldenCorpusLayoutError(InvalidInputError):
    """A golden corpus on disk does not match its declared layout."""

    code: ClassVar[str] = "INVALID_INPUT_GOLDEN_CORPUS_LAYOUT"


@dataclass(kw_only=True)
class GoldenCorpusFormatError(InvalidInputError):
    """A golden-corpus file is unparseable or does not hold records."""

    code: ClassVar[str] = "INVALID_INPUT_GOLDEN_CORPUS_FORMAT"


@dataclass(kw_only=True)
class GoldenParquetSupportError(PreconditionError):
    """A parquet corpus file was found without pyarrow installed."""

    code: ClassVar[str] = "PRECONDITION_GOLDEN_PARQUET_SUPPORT"


@dataclass(kw_only=True)
class GoldenCorpusUnavailableError(DependencyUnavailableError):
    """No golden corpus is configured — the skip case, not a failure."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_GOLDEN_CORPUS"


@dataclass(kw_only=True)
class GoldenDuplicateKeyError(InvalidInputError):
    """A golden-diff join key is not unique on one side — a wrong test, not a diff."""

    code: ClassVar[str] = "INVALID_INPUT_GOLDEN_DUPLICATE_KEY"


@dataclass(kw_only=True)
class GoldenRuleError(InvalidInputError):
    """A TypenameRule combines settings that contradict each other."""

    code: ClassVar[str] = "INVALID_INPUT_GOLDEN_RULE"
