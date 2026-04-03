"""Typed contracts for SQL metadata extraction.

These replace the ``Dict[str, Any]`` interfaces used by
``BaseSQLMetadataExtractionWorkflow`` and ``BaseSQLMetadataExtractionActivities``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import ConnectionRef, MaxItems
from application_sdk.credentials.ref import CredentialRef


class ExtractionInput(Input, allow_unbounded_fields=True):
    """Top-level input for a SQL metadata extraction run."""

    workflow_id: str = ""
    """Temporal workflow ID for this run."""

    connection: ConnectionRef = Field(default_factory=ConnectionRef)
    """Typed connection reference (qualified name, name, admin users, etc.)."""

    credential_guid: str = ""
    """GUID of credentials stored in the secret store."""

    credential_ref: CredentialRef | None = None
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


class ExtractionOutput(Output):
    """Top-level output from a SQL metadata extraction run.

    The ``transformed_data_prefix``, ``connection_qualified_name``,
    ``publish_state_prefix``, and ``current_state_prefix`` fields are read by
    the Automation Engine via JSONPath (``$.extract.outputs.*``) to feed the
    publish app.  They must be populated by the ``run()`` method.
    """

    workflow_id: str = ""
    success: bool = False
    databases_extracted: int = 0
    schemas_extracted: int = 0
    tables_extracted: int = 0
    views_extracted: int = 0
    columns_extracted: int = 0
    procedures_extracted: int = 0
    processes_extracted: int = 0
    records_uploaded: int = 0
    error: str = ""

    # ── Fields required by Automation Engine / publish app ───
    transformed_data_prefix: str = ""
    """Object-store-relative path to transformed JSONL files.

    Computed by stripping ``output_prefix`` from ``output_path`` and appending
    ``/transformed``.  Example: ``artifacts/apps/trino/workflows/{wf_id}/{run_id}/transformed``
    """

    connection_qualified_name: str = ""
    """Qualified name of the Atlan connection (e.g., ``default/trino/1234``)."""

    publish_state_prefix: str = ""
    """Object-store path where the publish app tracks publish state.

    Convention: ``persistent-artifacts/apps/atlan-publish-app/state/{connection_qn}/publish-state``
    """

    current_state_prefix: str = ""
    """Object-store path for the current extraction state.

    Convention: ``argo-artifacts/{connection_qn}/current-state``
    """


class ExtractionTaskInput(Input, allow_unbounded_fields=True):
    """Fields shared by all per-task inputs derived from ExtractionInput.

    Rather than passing a workflow_args dict[str, Any] blob, each task receives
    exactly the typed fields it needs, constructed from the top-level ExtractionInput
    by the run() method.
    """

    workflow_id: str = ""
    connection: ConnectionRef = Field(default_factory=ConnectionRef)
    credential_guid: str = ""
    credential_ref: CredentialRef | None = None
    output_prefix: str = ""
    output_path: str = ""
    exclude_filter: str = ""
    include_filter: str = ""
    temp_table_regex: str = ""
    source_tag_prefix: str = ""


class FetchDatabasesInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching databases from the source."""


class FetchDatabasesOutput(Output):
    """Output from fetching databases."""

    databases: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    chunk_count: int = 0
    total_record_count: int = 0


class FetchSchemasInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching schemas from the source."""


class FetchSchemasOutput(Output):
    """Output from fetching schemas."""

    schemas: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    chunk_count: int = 0
    total_record_count: int = 0


class FetchTablesInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching tables from the source."""


class FetchTablesOutput(Output):
    """Output from fetching tables."""

    tables: Annotated[list[str], MaxItems(100000)] = Field(default_factory=list)
    chunk_count: int = 0
    total_record_count: int = 0


class FetchColumnsInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching columns from the source."""


class FetchColumnsOutput(Output):
    """Output from fetching columns."""

    chunk_count: int = 0
    total_record_count: int = 0


class FetchProceduresInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching stored procedures from the source."""


class FetchProceduresOutput(Output):
    """Output from fetching stored procedures."""

    chunk_count: int = 0
    total_record_count: int = 0


class FetchViewsInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for fetching views from the source."""


class FetchViewsOutput(Output):
    """Output from fetching views."""

    chunk_count: int = 0
    total_record_count: int = 0


class TransformInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Input for the transform_data task."""

    typename: str = ""
    file_names: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    chunk_start: int = 0


class TransformOutput(Output):
    """Output from the transform_data task."""

    typename: str = ""
    total_record_count: int = 0
    chunk_count: int = 0
