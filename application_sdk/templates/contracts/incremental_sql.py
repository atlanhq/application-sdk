"""Typed contracts for incremental SQL metadata extraction.

Provides the full contract hierarchy for the v3 incremental extraction template,
including the ``IncrementalRunContext`` local accumulator and all per-task
input/output types.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from application_sdk.contracts.base import Input, Output
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    ExtractionTaskInput,
    FetchColumnsOutput,
    FetchDatabasesOutput,
    FetchSchemasOutput,
    FetchTablesOutput,
)

# =============================================================================
# IncrementalRunContext — local accumulator, NOT a Temporal payload
# =============================================================================


@dataclasses.dataclass
class IncrementalRunContext:
    """Incremental extraction state accumulated across tasks within a single run().

    This context is a local variable in run() — NOT stored in app_state, NOT a
    Temporal payload. It is built progressively as task outputs are collected.
    Temporal's workflow replay reconstructs it deterministically from recorded
    activity outputs, so it is crash-safe.
    """

    workflow_id: str = ""
    workflow_run_id: str = ""
    output_prefix: str = ""
    output_path: str = ""
    connection_qualified_name: str = ""
    connection_name: str = ""
    application_name: str = ""
    incremental_extraction: bool = False
    column_batch_size: int = 25000
    column_chunk_size: int = 100000
    copy_workers: int = 3
    prepone_marker_timestamp: bool = True
    prepone_marker_hours: int = 3
    # Set by fetch_incremental_marker
    marker_timestamp: Optional[str] = None
    next_marker_timestamp: str = ""
    # Set by read_current_state
    current_state_available: bool = False
    current_state_path: str = ""
    current_state_s3_prefix: str = ""
    current_state_json_count: int = 0
    # Set by write_current_state
    current_state_files: int = 0
    incremental_diff_path: str = ""
    incremental_diff_s3_prefix: str = ""
    incremental_diff_files: int = 0
    # Set by prepare_column_extraction_queries
    total_column_batches: int = 0
    changed_tables: int = 0
    backfill_tables: int = 0

    def is_incremental_ready(self) -> bool:
        """Return True when all prerequisites for incremental mode are satisfied.

        Requires: incremental_extraction=True AND a marker from a previous run AND
        a current-state snapshot from that run. On the very first run, marker_timestamp
        is None and current_state_available is False, so this returns False and the
        run performs a full extraction — building the initial state for future runs.
        """
        return bool(
            self.incremental_extraction
            and self.marker_timestamp
            and self.current_state_available
        )


# =============================================================================
# Top-level input/output for IncrementalSqlMetadataExtractor.run()
# =============================================================================


class IncrementalExtractionInput(ExtractionInput, allow_unbounded_fields=True):
    """Top-level input for an incremental SQL metadata extraction run.

    Extends :class:`ExtractionInput` with incremental-specific configuration.
    """

    incremental_extraction: bool = False
    """Enable incremental extraction mode."""

    column_batch_size: int = 25000
    """Number of tables per batch for incremental column extraction."""

    column_chunk_size: int = 100000
    """Number of column records per output chunk file."""

    copy_workers: int = 3
    """Parallel workers for file copy operations during state snapshot."""

    prepone_marker_timestamp: bool = True
    """Whether to move the marker back by ``prepone_marker_hours``."""

    prepone_marker_hours: int = 3
    """Hours to subtract from the marker when preponing is enabled."""


class IncrementalExtractionOutput(ExtractionOutput):
    """Top-level output from an incremental SQL metadata extraction run.

    Extends :class:`ExtractionOutput` with incremental-specific statistics.
    """

    current_state_files: int = 0
    """Number of files written to the current-state snapshot."""

    incremental_diff_files: int = 0
    """Number of files in the incremental diff (0 on first run)."""

    column_batches_executed: int = 0
    """Number of incremental column batches executed."""

    changed_tables: int = 0
    """Tables detected as changed since last run."""

    backfill_tables: int = 0
    """Tables detected as needing backfill (new tables)."""

    marker_updated: bool = False
    """Whether the incremental marker was updated after this run."""


# =============================================================================
# Shared incremental task input base
# =============================================================================


class IncrementalTaskInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Base task input with incremental runtime state.

    Extends :class:`ExtractionTaskInput` with the incremental fields that
    every incremental task needs to decide between full and incremental paths.
    """

    incremental_extraction: bool = False
    """Whether incremental extraction is enabled for this run."""

    marker_timestamp: str = ""
    """Marker timestamp from previous run; empty string means full extraction."""

    current_state_available: bool = False
    """Whether a current-state snapshot from a previous run is available."""

    column_chunk_size: int = 100000
    """Number of column records per output chunk file."""


# =============================================================================
# Per-task incremental input types (inherit IncrementalTaskInput)
# =============================================================================


class FetchTablesIncrementalInput(IncrementalTaskInput, allow_unbounded_fields=True):
    """Input for the incremental fetch_tables task.

    Carries all fields from :class:`IncrementalTaskInput`. The marker and
    current_state_available fields together indicate whether to use the
    incremental or full-extraction SQL path — see the ``fetch_tables``
    docstring in :class:`IncrementalSqlMetadataExtractor` for details.
    """


class FetchColumnsIncrementalInput(IncrementalTaskInput, allow_unbounded_fields=True):
    """Input for the incremental fetch_columns task.

    When both ``marker_timestamp`` is non-empty and ``current_state_available``
    is True, the base ``fetch_columns`` implementation returns immediately with
    zero counts — column extraction is delegated to batch tasks instead.
    """


# =============================================================================
# fetch_incremental_marker task
# =============================================================================


class FetchIncrementalMarkerInput(Input, allow_unbounded_fields=True):
    """Input for the fetch_incremental_marker task."""

    connection_qualified_name: str = ""
    """Connection qualified name used to locate the persistent marker file."""

    application_name: str = ""
    """Application name for S3 path resolution."""

    existing_marker: Optional[str] = None
    """Pre-existing marker value (e.g., from a manual workflow override)."""

    prepone_enabled: bool = True
    """Whether to move the marker back by ``prepone_hours``."""

    prepone_hours: float = 3.0
    """Hours to subtract from the marker when preponing is enabled."""


class FetchIncrementalMarkerOutput(Output):
    """Output from the fetch_incremental_marker task."""

    marker_timestamp: str = ""
    """Processed marker from the previous run; empty on the first run."""

    next_marker_timestamp: str = ""
    """New marker timestamp generated for the current run."""


# =============================================================================
# read_current_state task
# =============================================================================


class ReadCurrentStateInput(Input, allow_unbounded_fields=True):
    """Input for the read_current_state task."""

    connection_qualified_name: str = ""
    application_name: str = ""


class ReadCurrentStateOutput(Output):
    """Output from the read_current_state task."""

    current_state_path: str = ""
    """Local filesystem path where the current state was downloaded."""

    current_state_s3_prefix: str = ""
    """S3 prefix for the current-state folder."""

    current_state_available: bool = False
    """Whether a non-empty current-state snapshot was found."""

    current_state_json_count: int = 0
    """Number of JSON files in the downloaded current state."""


# =============================================================================
# prepare_column_extraction_queries task
# =============================================================================


class PrepareColumnQueriesInput(IncrementalTaskInput, allow_unbounded_fields=True):
    """Input for the prepare_column_extraction_queries task."""

    connection_qualified_name: str = ""
    """Connection qualified name for persistent artifact path resolution."""

    current_state_s3_prefix: str = ""
    """S3 prefix of the current-state snapshot for backfill comparison."""

    column_batch_size: int = 25000
    """Number of tables per batch file."""

    application_name: str = ""


class PrepareColumnQueriesOutput(Output):
    """Output from the prepare_column_extraction_queries task."""

    total_batches: int = 0
    changed_tables: int = 0
    backfill_tables: int = 0
    total_tables: int = 0
    batches_s3_prefix: str = ""
    batches_local_dir: str = ""


# =============================================================================
# execute_single_column_batch task
# =============================================================================


class ExecuteColumnBatchInput(IncrementalTaskInput, allow_unbounded_fields=True):
    """Input for executing a single incremental column batch."""

    batch_index: int = 0
    """Zero-based index of this batch within the total."""

    total_batches: int = 1
    """Total number of batches."""

    batches_s3_prefix: str = ""
    """S3 prefix where the batch JSON files are stored."""

    application_name: str = ""


class ExecuteColumnBatchOutput(Output):
    """Output from executing a single incremental column batch."""

    batch_index: int = 0
    records: int = 0
    status: str = ""


# =============================================================================
# write_current_state task
# =============================================================================


class WriteCurrentStateInput(IncrementalTaskInput, allow_unbounded_fields=True):
    """Input for the write_current_state task."""

    workflow_run_id: str = ""
    """Temporal run ID used to name the incremental diff subfolder."""

    current_state_s3_prefix: str = ""
    """S3 prefix for the existing current-state (for previous-state download)."""

    copy_workers: int = 3
    """Parallel workers for file copy operations."""

    application_name: str = ""


class WriteCurrentStateOutput(Output):
    """Output from the write_current_state task."""

    current_state_path: str = ""
    current_state_s3_prefix: str = ""
    current_state_files: int = 0
    incremental_diff_path: str = ""
    incremental_diff_s3_prefix: str = ""
    incremental_diff_files: int = 0


# =============================================================================
# update_incremental_marker task
# =============================================================================


class UpdateMarkerInput(Input, allow_unbounded_fields=True):
    """Input for the update_incremental_marker task."""

    connection_qualified_name: str = ""
    next_marker_timestamp: str = ""
    application_name: str = ""


class UpdateMarkerOutput(Output):
    """Output from the update_incremental_marker task."""

    marker_written: bool = False
    marker_timestamp: str = ""
    s3_key: str = ""


# =============================================================================
# Re-export base types used in incremental contracts for convenience
# =============================================================================

__all__ = [
    # Context (not a Temporal payload)
    "IncrementalRunContext",
    # Top-level run() contracts
    "IncrementalExtractionInput",
    "IncrementalExtractionOutput",
    # Shared incremental task base
    "IncrementalTaskInput",
    # Per-task incremental inputs (override of base extraction tasks)
    "FetchTablesIncrementalInput",
    "FetchColumnsIncrementalInput",
    # fetch_incremental_marker
    "FetchIncrementalMarkerInput",
    "FetchIncrementalMarkerOutput",
    # read_current_state
    "ReadCurrentStateInput",
    "ReadCurrentStateOutput",
    # prepare_column_extraction_queries
    "PrepareColumnQueriesInput",
    "PrepareColumnQueriesOutput",
    # execute_single_column_batch
    "ExecuteColumnBatchInput",
    "ExecuteColumnBatchOutput",
    # write_current_state
    "WriteCurrentStateInput",
    "WriteCurrentStateOutput",
    # update_incremental_marker
    "UpdateMarkerInput",
    "UpdateMarkerOutput",
    # Re-exported base output types (used in incremental extractor signatures)
    "FetchDatabasesOutput",
    "FetchSchemasOutput",
    "FetchTablesOutput",
    "FetchColumnsOutput",
]
