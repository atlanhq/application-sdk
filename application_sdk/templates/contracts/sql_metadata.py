"""Typed contracts for SQL metadata extraction.

These replace the ``Dict[str, Any]`` interfaces used by
``BaseSQLMetadataExtractionWorkflow`` and ``BaseSQLMetadataExtractionActivities``.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Annotated, Any

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import MaxItems

if TYPE_CHECKING:
    from application_sdk.credentials import CredentialRef


@dataclasses.dataclass
class ExtractionInput(Input, allow_unbounded_fields=True):
    """Top-level input for a SQL metadata extraction run."""

    workflow_id: str = ""
    """Temporal workflow ID for this run."""

    connection: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Connection metadata (qualified name, name, etc.)."""

    credential_guid: str = ""
    """GUID of credentials stored in the secret store."""

    credential_ref: "CredentialRef | None" = None
    """Typed credential reference — preferred over credential_guid for new apps."""

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
class ExtractionTaskInput(Input, allow_unbounded_fields=True):
    """Fields shared by all per-task inputs derived from ExtractionInput.

    Rather than passing a workflow_args dict[str, Any] blob, each task receives
    exactly the typed fields it needs, constructed from the top-level ExtractionInput
    by the run() method.
    """

    workflow_id: str = ""
    connection: dict[str, Any] = dataclasses.field(default_factory=dict)
    credential_guid: str = ""
    credential_ref: "CredentialRef | None" = None
    output_prefix: str = ""
    output_path: str = ""
    exclude_filter: str = ""
    include_filter: str = ""
    temp_table_regex: str = ""
    source_tag_prefix: str = ""


@dataclasses.dataclass
class FetchDatabasesInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching databases from the source."""


@dataclasses.dataclass
class FetchDatabasesOutput(Output):
    """Output from fetching databases."""

    databases: Annotated[list[str], MaxItems(10000)] = dataclasses.field(
        default_factory=list
    )
    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class FetchSchemasInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching schemas from the source."""


@dataclasses.dataclass
class FetchSchemasOutput(Output):
    """Output from fetching schemas."""

    schemas: Annotated[list[str], MaxItems(10000)] = dataclasses.field(
        default_factory=list
    )
    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class FetchTablesInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching tables from the source."""


@dataclasses.dataclass
class FetchTablesOutput(Output):
    """Output from fetching tables."""

    tables: Annotated[list[str], MaxItems(100000)] = dataclasses.field(
        default_factory=list
    )
    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class FetchColumnsInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching columns from the source."""


@dataclasses.dataclass
class FetchColumnsOutput(Output):
    """Output from fetching columns."""

    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class FetchProceduresInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching stored procedures from the source."""


@dataclasses.dataclass
class FetchProceduresOutput(Output):
    """Output from fetching stored procedures."""

    chunk_count: int = 0
    total_record_count: int = 0


@dataclasses.dataclass
class TransformInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for the transform_data task."""

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
