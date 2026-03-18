"""Typed contracts for SQL metadata extraction.

These replace the ``Dict[str, Any]`` interfaces used by
``BaseSQLMetadataExtractionWorkflow`` and ``BaseSQLMetadataExtractionActivities``.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import MaxItems


@dataclasses.dataclass
class ExtractionInput(Input, allow_unbounded_fields=True):
    """Top-level input for a SQL metadata extraction run."""

    workflow_id: str = ""
    """Temporal workflow ID for this run."""

    connection: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Connection metadata (qualified name, name, etc.)."""

    credential_guid: str = ""
    """GUID of credentials stored in the secret store."""

    output_prefix: str = ""
    """Object store prefix for all output artifacts."""

    output_path: str = ""
    """Local or object store path for output files."""

    exclude_filter: str = ""
    """Regex filter for excluding schemas/tables."""

    include_filter: str = ""
    """Regex filter for including schemas/tables."""

    temp_table_regex: str = ""
    """Regex pattern identifying temporary tables."""

    source_tag_prefix: str = ""
    """Tag prefix for source-level metadata."""


@dataclasses.dataclass
class ExtractionOutput(Output):
    """Top-level output from a SQL metadata extraction run."""

    workflow_id: str = ""
    success: bool = False
    databases_extracted: int = 0
    schemas_extracted: int = 0
    tables_extracted: int = 0
    columns_extracted: int = 0
    records_uploaded: int = 0
    error: str = ""


@dataclasses.dataclass
class FetchDatabasesInput(Input, allow_unbounded_fields=True):
    """Input for fetching databases from the source."""

    workflow_args: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Full workflow args dict passed through from ExtractionInput."""


@dataclasses.dataclass
class FetchDatabasesOutput(Output):
    """Output from fetching databases."""

    databases: Annotated[list[str], MaxItems(10000)] = dataclasses.field(
        default_factory=list
    )
    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class FetchSchemasInput(Input, allow_unbounded_fields=True):
    """Input for fetching schemas from the source."""

    workflow_args: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class FetchSchemasOutput(Output):
    """Output from fetching schemas."""

    schemas: Annotated[list[str], MaxItems(10000)] = dataclasses.field(
        default_factory=list
    )
    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class FetchTablesInput(Input, allow_unbounded_fields=True):
    """Input for fetching tables from the source."""

    workflow_args: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class FetchTablesOutput(Output):
    """Output from fetching tables."""

    tables: Annotated[list[str], MaxItems(100000)] = dataclasses.field(
        default_factory=list
    )
    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class FetchColumnsInput(Input, allow_unbounded_fields=True):
    """Input for fetching columns from the source."""

    workflow_args: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class FetchColumnsOutput(Output):
    """Output from fetching columns."""

    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class TransformInput(Input, allow_unbounded_fields=True):
    """Input for the transform_data task."""

    workflow_args: dict[str, Any] = dataclasses.field(default_factory=dict)
    typename: str = ""
    file_names: Annotated[list[str], MaxItems(10000)] = dataclasses.field(
        default_factory=list
    )
    chunk_start: int = 0


@dataclasses.dataclass
class TransformOutput(Output):
    """Output from the transform_data task."""

    typename: str = ""
    total_record_count: int = 0
    chunk_count: int = 0
