"""Typed error leaves for SQL and incremental extraction templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    InternalError,
    InvalidInputError,
    UnimplementedError,
)


@dataclass(kw_only=True)
class SqlQueryExtractorNotImplementedError(UnimplementedError):
    """Abstract SqlQueryExtractor method was not overridden by a subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_QUERY_EXTRACTOR_METHOD"
    message: str = "SqlQueryExtractor subclass did not implement required method"
    component: str | None = "sql_query_extractor"


@dataclass(kw_only=True)
class SqlMetadataExtractionError(InternalError):
    """SQL metadata extraction workflow failed."""

    code: ClassVar[str] = "INTERNAL_SQL_METADATA_EXTRACTION"
    message: str = "SQL metadata extraction failed"
    component: str | None = "sql_metadata_extractor"
    workflow_id: str | None = None


@dataclass(kw_only=True)
class SqlQueryExtractionError(InternalError):
    """SQL query extraction workflow failed."""

    code: ClassVar[str] = "INTERNAL_SQL_QUERY_EXTRACTION"
    message: str = "SQL query extraction failed"
    component: str | None = "sql_query_extractor"
    workflow_id: str | None = None


@dataclass(kw_only=True)
class IncrementalStateWriteError(InternalError):
    """Writing the incremental current-state snapshot failed."""

    code: ClassVar[str] = "INTERNAL_INCREMENTAL_STATE_WRITE"
    message: str = "Failed to write current-state"
    component: str | None = "incremental_sql_metadata_extractor"


@dataclass(kw_only=True)
class SqlMetadataExtractorNotImplementedError(UnimplementedError):
    """Abstract SqlMetadataExtractor method was not overridden by a subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_SQL_METADATA_EXTRACTOR_METHOD"
    message: str = "SqlMetadataExtractor subclass did not implement required method"
    component: str | None = "sql_metadata_extractor"


@dataclass(kw_only=True)
class IncrementalSqlMetadataExtractorNotImplementedError(UnimplementedError):
    """Abstract IncrementalSqlMetadataExtractor method was not overridden by a subclass."""

    code: ClassVar[str] = "UNIMPLEMENTED_INCREMENTAL_SQL_METADATA_EXTRACTOR_METHOD"
    message: str = (
        "IncrementalSqlMetadataExtractor subclass did not implement required method"
    )
    component: str | None = "incremental_sql_metadata_extractor"


@dataclass(kw_only=True)
class SqlCredentialRefMissingError(InvalidInputError):
    """No credential reference or GUID available in the task input."""

    code: ClassVar[str] = "INVALID_INPUT_SQL_CREDENTIAL_REF_MISSING"
    message: str = "No credential reference or GUID available in task input"
    field: str | None = "credential_ref"


@dataclass(kw_only=True)
class SqlSecretStoreMissingError(InvalidInputError):
    """No secret store available for SQL credential resolution."""

    code: ClassVar[str] = "INVALID_INPUT_SQL_SECRET_STORE_MISSING"
    message: str = "No secret store available for credential resolution"
    field: str | None = "secret_store"
