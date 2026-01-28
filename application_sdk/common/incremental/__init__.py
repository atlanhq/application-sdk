"""Incremental extraction utilities for SQL metadata extraction.

This module provides shared utilities for incremental metadata extraction:
- Models: WorkflowMetadata, TableScope, MergeResult, IncrementalDiffResult
- Helpers: Marker handling, S3 path management, state management
- Storage: DuckDB and RocksDB utilities for efficient data processing

Usage:
    from application_sdk.common.incremental.models import (
        WorkflowMetadata,
        IncrementalWorkflowArgs,
        TableScope,
    )
    from application_sdk.common.incremental.helpers import (
        is_incremental_ready,
        get_persistent_s3_prefix,
    )
"""

from application_sdk.common.incremental.models import (
    EntityType,
    ColumnField,
    ConnectionInfo,
    WorkflowMetadata,
    TableScope,
    MergeResult,
    IncrementalDiffResult,
    IncrementalWorkflowArgs,
)
from application_sdk.common.incremental.constants import (
    PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE,
    MAX_CONCURRENT_COLUMN_BATCHES,
    INCREMENTAL_DIFF_SUBPATH_TEMPLATE,
    MARKER_TIMESTAMP_FORMAT,
    INCREMENTAL_DEFAULT_STATE,
    DUCKDB_COMMON_TEMP_FOLDER,
    DUCKDB_DEFAULT_MEMORY_LIMIT,
)

__all__ = [
    # Models
    "EntityType",
    "ColumnField",
    "ConnectionInfo",
    "WorkflowMetadata",
    "TableScope",
    "MergeResult",
    "IncrementalDiffResult",
    "IncrementalWorkflowArgs",
    # Constants
    "PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE",
    "MAX_CONCURRENT_COLUMN_BATCHES",
    "INCREMENTAL_DIFF_SUBPATH_TEMPLATE",
    "MARKER_TIMESTAMP_FORMAT",
    "INCREMENTAL_DEFAULT_STATE",
    "DUCKDB_COMMON_TEMP_FOLDER",
    "DUCKDB_DEFAULT_MEMORY_LIMIT",
]
