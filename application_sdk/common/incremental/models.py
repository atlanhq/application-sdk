"""Pydantic models for incremental metadata extraction workflow arguments.

This module provides type-safe models for workflow arguments used in incremental
metadata extraction workflows.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set, Union

from pydantic import BaseModel, Field

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
    """Connection information for the database."""

    connection_name: Optional[str] = None
    connection_qualified_name: Optional[str] = None


class WorkflowMetadata(BaseModel):
    """Metadata configuration for the incremental extraction workflow.

    This model handles the conversion of string values from Argo to proper Python types.
    All boolean fields accept both actual booleans and string representations like "true"/"false".

    Attributes:
        incremental_extraction: Whether to run incremental extraction
        column_batch_size: Number of tables per batch for column extraction
        column_chunk_size: Number of column records per output chunk
        copy_workers: Number of parallel workers for file copy operations (default: 3)
        prepone_marker_timestamp: Whether to move marker back by prepone_marker_hours
        prepone_marker_hours: Hours to move marker back when prepone_marker_timestamp is True
        marker_timestamp: Timestamp from previous successful run (for incremental)
        next_marker_timestamp: Timestamp to use for current run
        current_state_available: Whether previous state exists for backfill comparison
        current_state_path: Local path to current state directory
        current_state_s3_prefix: S3 prefix for current state folder
        current_state_json_count: Number of JSON files in current state
        current_state_files: Number of files copied to current state
        incremental_diff_path: Local path to incremental diff directory
        incremental_diff_s3_prefix: S3 prefix for incremental diff folder
        incremental_diff_files: Number of files in incremental diff
    """

    # Configuration from Argo - accept both hyphenated and underscore versions
    incremental_extraction: bool = Field(default=False, alias="incremental-extraction")
    column_batch_size: int = Field(default=25000, alias="column-batch-size", gt=0)
    column_chunk_size: int = Field(default=100000, alias="column-chunk-size", gt=0)

    # Marker timestamp adjustment settings
    prepone_marker_timestamp: bool = Field(
        default=True, alias="prepone-marker-timestamp"
    )
    prepone_marker_hours: int = Field(default=3, alias="prepone-marker-hours")

    # Copy workers - number of parallel workers for file copy operations
    copy_workers: int = Field(default=3, alias="copy-workers", gt=0)

    # Filters (kept with hyphens as they come from Argo)
    include_filter: Optional[Union[Dict[str, Any], str]] = Field(
        default=None, alias="include-filter"
    )
    exclude_filter: Optional[Union[Dict[str, Any], str]] = Field(
        default=None, alias="exclude-filter"
    )
    temp_table_regex: Optional[str] = Field(default="", alias="temp-table-regex")
    extraction_method: Optional[str] = Field(
        default="direct", alias="extraction-method"
    )

    # Runtime state (set during workflow execution)
    marker_timestamp: Optional[str] = None
    next_marker_timestamp: Optional[str] = None
    current_state_available: bool = False
    current_state_path: Optional[str] = None
    current_state_s3_prefix: Optional[str] = None
    current_state_json_count: int = 0
    current_state_files: int = 0

    # Incremental diff state (set during write_current_state)
    incremental_diff_path: Optional[str] = None
    incremental_diff_s3_prefix: Optional[str] = None
    incremental_diff_files: int = 0

    model_config = {
        "populate_by_name": True,  # Allow both alias and field name
        "extra": "allow",  # Allow extra fields for forward compatibility
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
    extraction include/exclude filters. This is a pure data model - use helper
    functions from table_scope.py for operations.

    For large datasets (millions of tables), uses:
    - Set[str] for table_qualified_names (in-memory, ~10MB for 100K tables)
    - Rdict (RocksDB) for table_states (disk-backed state storage with Bloom filter)

    Attributes:
        table_qualified_names: Set of all table qualified names in current extraction
        table_states: Mapping of qualified name to incremental state
        tables_with_extracted_columns: Set of table qualified names that have columns
        state_counts: Internal cache of state counts (CREATED/UPDATED/NO CHANGE)
    """

    table_qualified_names: Set[str] = Field(default_factory=set)
    table_states: Any = Field(  # Rdict type
        default_factory=create_states_db,
        exclude=True,
    )
    tables_with_extracted_columns: Set[str] = Field(default_factory=set)
    state_counts: Dict[str, int] = Field(default_factory=dict, exclude=True)

    model_config = {
        "arbitrary_types_allowed": True,  # Allow Rdict type
    }


class MergeResult(BaseModel):
    """Result of ancestral column merge operation.

    Tracks counts of columns merged from current extraction and ancestral state,
    as well as columns excluded.

    Attributes:
        columns_from_current: Count of columns from current extraction
        columns_from_ancestral: Count of columns carried from ancestral state
        columns_total: Total columns in merged output
        excluded_already_extracted: Ancestral columns skipped because parent table
            was CREATED/UPDATED in current run (fresh columns were extracted)
        excluded_table_removed: Ancestral columns excluded because parent table
            no longer exists in extraction scope
    """

    columns_from_current: int = 0
    columns_from_ancestral: int = 0
    columns_total: int = 0
    excluded_already_extracted: int = 0
    excluded_table_removed: int = 0


class IncrementalDiffResult(BaseModel):
    """Result of incremental diff creation.

    Tracks counts of changed assets written to incremental-diff folder.
    This folder contains only assets that changed in this specific run
    (CREATED, UPDATED, BACKFILL, or DELETED).

    Attributes:
        tables_created: Count of CREATED tables
        tables_updated: Count of UPDATED tables
        tables_backfill: Count of BACKFILL tables
        tables_deleted: Count of DELETED tables (in previous but not in current)
        columns_total: Total columns for changed tables
        columns_deleted: Total deleted columns (from deleted/updated tables)
        schemas_total: Total schemas in diff
        databases_total: Total databases in diff
        total_files: Total JSON files written
        is_incremental: Whether this diff was produced from incremental extraction
    """

    tables_created: int = 0
    tables_updated: int = 0
    tables_backfill: int = 0
    tables_deleted: int = 0
    columns_total: int = 0
    columns_deleted: int = 0
    schemas_total: int = 0
    databases_total: int = 0
    total_files: int = 0
    is_incremental: bool = True

    @property
    def total_changed_entities(self) -> int:
        """Total number of changed entities (tables + columns, including deletes)."""
        return (
            self.tables_created
            + self.tables_updated
            + self.tables_backfill
            + self.tables_deleted
            + self.columns_total
            + self.columns_deleted
            + self.schemas_total
            + self.databases_total
        )


class IncrementalWorkflowArgs(BaseModel):
    """Complete workflow arguments for incremental metadata extraction.

    This is the top-level model that encompasses all workflow configuration.

    Attributes:
        workflow_id: Unique identifier for the workflow
        workflow_run_id: Unique identifier for this specific run
        output_prefix: Base path for output files
        output_path: Full path for this run's output
        connection: Connection information
        metadata: Extraction metadata and configuration
        credentials: Database credentials (not modeled for security)
        application_name: Name of the application
        typename: Current entity type being processed
        file_names: Specific files to process
        chunk_start: Starting chunk number
        chunk_size: Size of chunks for processing
    """

    workflow_id: Optional[str] = None
    workflow_run_id: Optional[str] = None
    output_prefix: Optional[str] = None
    output_path: Optional[str] = None
    connection: ConnectionInfo = Field(default_factory=ConnectionInfo)
    metadata: WorkflowMetadata = Field(default_factory=WorkflowMetadata)

    # Runtime fields
    application_name: Optional[str] = None
    typename: Optional[str] = None
    file_names: Optional[List[str]] = None
    chunk_start: Optional[int] = None
    chunk_size: Optional[int] = None

    # Pass through credentials without modeling (security)
    credentials: Optional[Dict[str, Any]] = None

    model_config = {
        "extra": "allow",  # Allow extra fields for forward compatibility
    }

    def is_incremental_ready(self) -> bool:
        """Check if all prerequisites for incremental extraction are met."""
        return self.metadata.is_incremental_ready()
