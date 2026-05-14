"""Typed error leaves for the incremental processing subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DependencyUnavailableError,
    InternalError,
    InvalidInputError,
    PreconditionError,
)


@dataclass(kw_only=True)
class MarkerUploadError(DependencyUnavailableError):
    """Incremental marker could not be uploaded to S3."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_MARKER_S3"
    message: str = "Failed to upload marker to S3"
    service: str | None = "s3"


@dataclass(kw_only=True)
class TableScopeLoadError(InternalError):
    """Incremental table scope could not be loaded."""

    code: ClassVar[str] = "INTERNAL_INCREMENTAL_TABLE_SCOPE"
    message: str = "Failed to load table scope"
    component: str | None = "table_scope"


@dataclass(kw_only=True)
class StateDownloadError(DependencyUnavailableError):
    """Previous incremental state could not be downloaded."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_STATE_DOWNLOAD"
    message: str = "Failed to download previous state"
    service: str | None = "object_store"


@dataclass(kw_only=True)
class JsonScanError(InternalError):
    """Scanning JSON files in the incremental base directory failed."""

    code: ClassVar[str] = "INTERNAL_INCREMENTAL_JSON_SCAN"
    message: str = "Failed to scan JSON files"
    component: str | None = "backfill"
    base_dir: str | None = None


@dataclass(kw_only=True)
class DaftAnalysisError(InternalError):
    """Daft table analysis failed during incremental column extraction."""

    code: ClassVar[str] = "INTERNAL_INCREMENTAL_DAFT_ANALYSIS"
    message: str = "Daft table analysis failed"
    component: str | None = "column_extraction"


@dataclass(kw_only=True)
class ConnectionQualifiedNameEmptyError(InvalidInputError):
    """connection_qualified_name was provided but is empty."""

    code: ClassVar[str] = "INVALID_INPUT_INCREMENTAL_CONN_QN_EMPTY"
    message: str = "connection_qualified_name cannot be empty"
    field: str | None = "connection_qualified_name"


@dataclass(kw_only=True)
class ConnectionQualifiedNameFormatError(InvalidInputError):
    """connection_qualified_name does not match the expected tenant/connector/epoch format."""

    code: ClassVar[str] = "INVALID_INPUT_INCREMENTAL_CONN_QN_FORMAT"
    message: str = "Could not extract epoch ID from connection_qualified_name"
    field: str | None = "connection_qualified_name"


@dataclass(kw_only=True)
class ConnectionQualifiedNameMissingError(InvalidInputError):
    """connection_qualified_name was not provided at all."""

    code: ClassVar[str] = "INVALID_INPUT_INCREMENTAL_CONN_QN_MISSING"
    message: str = "connection_qualified_name is required"
    field: str | None = "connection_qualified_name"


@dataclass(kw_only=True)
class DuckDbConnectionClosedError(PreconditionError):
    """Operation attempted on a DuckDB connection that has already been closed."""

    code: ClassVar[str] = "PRECONDITION_DUCKDB_CONNECTION_CLOSED"
    message: str = "DuckDB connection has been closed"
    resource: str | None = "duckdb_connection"
    expected_state: str | None = "open"
    actual_state: str | None = "closed"


@dataclass(kw_only=True)
class ColumnBatchInputError(InvalidInputError):
    """No transformed tables were found when backfill analysis requires them."""

    code: ClassVar[str] = "INVALID_INPUT_INCREMENTAL_COLUMN_BATCH"
    message: str = "No transformed tables found for finding backfill tables"
    field: str | None = "current_transformed_dir"
