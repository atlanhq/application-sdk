"""Pydantic models for incremental extraction workflow arguments.

This module provides type-safe models for:
- WorkflowMetadata: Configuration and runtime state for incremental extraction
- TableScope: Current extracted assets with include/exclude filters
- MergeResult: Result of ancestral column merge operation
- IncrementalDiffResult: Result of incremental diff creation
- IncrementalWorkflowArgs: Complete workflow arguments
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set, Union

from pydantic import BaseModel, Field
from rocksdict import Rdict

from application_sdk.common.incremental.storage.rocksdb_utils import create_states_db


class EntityType(str, Enum):
    """Metadata entity types used across the extraction workflow."""

    TABLE = "table"
    COLUMN = "column"
    SCHEMA = "schema"
    DATABASE = "database"


class ColumnField(str, Enum):
    """Standard column fields used in ancestral merge operations."""

    TYPE_NAME = "typeName"
    STATUS = "status"
    ATTRIBUTES = "attributes"
    CUSTOM_ATTRIBUTES = "customAttributes"


class ConnectionInfo(BaseModel):
    """Connection information from workflow configuration.

    Attributes:
        connection_qualified_name: Full qualified name (e.g., "tenant/connector/epoch")
        connection_name: Human-readable connection name
    """

    connection_qualified_name: str = Field(
        default="", alias="connection-qualified-name"
    )
    connection_name: str = Field(default="", alias="connection-name")

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }


class WorkflowMetadata(BaseModel):
    """Metadata configuration for the extraction workflow.

    This model handles the conversion of string values from Argo to proper Python types.
    All boolean fields accept both actual booleans and string representations.

    Attributes:
        incremental_extraction: Whether to run incremental extraction
        column_batch_size: Number of tables per batch for column extraction
        column_chunk_size: Number of column records per output chunk
        copy_workers: Number of parallel workers for file copy operations
        prepone_marker_timestamp: Whether to move marker back by prepone_marker_hours
        prepone_marker_hours: Hours to move marker back when prepone_marker_timestamp is True
        include_filter: Filter for including specific schemas
        exclude_filter: Filter for excluding specific schemas
        temp_table_regex: Regex pattern for excluding temporary tables
        system_schema_name: Database system schema name (default: SYS)
        marker_timestamp: Timestamp from previous successful run
        next_marker_timestamp: Timestamp to use for current run
        current_state_available: Whether previous state exists
        current_state_path: Local path to current state directory
        current_state_s3_prefix: S3 prefix for current state folder
        current_state_json_count: Number of JSON files in current state
        current_state_files: Number of files copied to current state
        incremental_diff_path: Local path to incremental diff directory
        incremental_diff_s3_prefix: S3 prefix for incremental diff folder
        incremental_diff_files: Number of files in incremental diff
        extraction_method: Extraction method (direct, s3, agent)
    """

    # Configuration from Argo
    incremental_extraction: bool = Field(
        default=False, alias="incremental-extraction"
    )
    column_batch_size: int = Field(default=25000, alias="column-batch-size")
    column_chunk_size: int = Field(default=100000, alias="column-chunk-size")
    system_schema_name: str = Field(default="SYS", alias="system-schema-name")

    # Marker timestamp adjustment settings
    prepone_marker_timestamp: bool = Field(
        default=True, alias="prepone-marker-timestamp"
    )
    prepone_marker_hours: int = Field(default=3, alias="prepone-marker-hours")

    # Copy workers for file operations
    copy_workers: int = Field(default=3, alias="copy-workers")

    # Filters
    include_filter: Optional[Union[Dict[str, Any], str]] = Field(
        default=None, alias="include-filter"
    )
    exclude_filter: Optional[Union[Dict[str, Any], str]] = Field(
        default=None, alias="exclude-filter"
    )
    temp_table_regex: Optional[str] = Field(default="", alias="temp-table-regex")
    extraction_method: Optional[str] = Field(default="direct", alias="extraction-method")

    # Runtime state (set during workflow execution)
    marker_timestamp: Optional[str] = None
    next_marker_timestamp: Optional[str] = None
    current_state_available: bool = False
    current_state_path: Optional[str] = None
    current_state_s3_prefix: Optional[str] = None
    current_state_json_count: int = 0
    current_state_files: int = 0
    # Incremental diff state
    incremental_diff_path: Optional[str] = None
    incremental_diff_s3_prefix: Optional[str] = None
    incremental_diff_files: int = 0

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }

    def is_incremental_ready(self) -> bool:
        """Check if all prerequisites for incremental extraction are met."""
        return bool(
            self.incremental_extraction
            and self.marker_timestamp
            and self.current_state_available
        )


class TableScope(BaseModel):
    """Current extracted assets with include/exclude filters.

    Used during current state merge to track which tables exist in the current
    extraction include/exclude filters.

    For large datasets (millions of tables), uses:
    - Set[str] for table_qualified_names (in-memory)
    - Rdict (RocksDB) for table_states (disk-backed)

    Attributes:
        table_qualified_names: Set of all table qualified names in current extraction
        table_states: Mapping of qualified name to incremental state
        tables_with_extracted_columns: Set of table qualified names with extracted columns
        state_counts: Internal cache of state counts
    """

    table_qualified_names: Set[str] = Field(default_factory=set)
    table_states: Rdict = Field(
        default_factory=create_states_db,
        exclude=True,
    )
    tables_with_extracted_columns: Set[str] = Field(default_factory=set)
    state_counts: Dict[str, int] = Field(default_factory=dict, exclude=True)

    model_config = {
        "arbitrary_types_allowed": True,
    }


class MergeResult(BaseModel):
    """Result of ancestral column merge operation.

    Attributes:
        columns_from_current: Count of columns from current extraction
        columns_from_ancestral: Count of columns carried from ancestral state
        columns_total: Total columns in merged output
        excluded_already_extracted: Ancestral columns skipped (table was re-extracted)
        excluded_table_removed: Ancestral columns excluded (table no longer in scope)
    """

    columns_from_current: int = 0
    columns_from_ancestral: int = 0
    columns_total: int = 0
    excluded_already_extracted: int = 0
    excluded_table_removed: int = 0


class IncrementalDiffResult(BaseModel):
    """Result of incremental diff creation.

    Attributes:
        tables_created: Count of CREATED tables
        tables_updated: Count of UPDATED tables
        tables_backfill: Count of BACKFILL tables
        columns_total: Total columns for changed tables
        schemas_total: Total schemas in diff
        databases_total: Total databases in diff
        total_files: Total JSON files written
    """

    tables_created: int = 0
    tables_updated: int = 0
    tables_backfill: int = 0
    columns_total: int = 0
    schemas_total: int = 0
    databases_total: int = 0
    total_files: int = 0


class IncrementalWorkflowArgs(BaseModel):
    """Complete workflow arguments for incremental extraction.

    This model wraps workflow_args dict with type-safe access to all fields.

    Attributes:
        workflow_id: Unique workflow identifier
        workflow_run_id: Current run identifier
        output_path: Local output path for extracted data
        application_name: Name of the application (e.g., "oracle")
        connection: Connection information
        metadata: Workflow metadata configuration
    """

    workflow_id: str = ""
    workflow_run_id: str = ""
    output_path: str = ""
    application_name: str = ""
    connection: ConnectionInfo = Field(default_factory=ConnectionInfo)
    metadata: WorkflowMetadata = Field(default_factory=WorkflowMetadata)

    model_config = {
        "populate_by_name": True,
        "extra": "allow",
    }

    @classmethod
    def from_dict(cls, workflow_args: Dict[str, Any]) -> "IncrementalWorkflowArgs":
        """Create IncrementalWorkflowArgs from a workflow_args dict."""
        return cls.model_validate(workflow_args)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for passing to activities."""
        return self.model_dump(by_alias=True, exclude_none=True)
