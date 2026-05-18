"""Typed error leaves for SQL and incremental extraction templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import InternalError


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
