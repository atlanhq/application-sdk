"""Incremental SQL metadata extraction activities.

This module provides the IncrementalSQLMetadataExtractionActivities class that extends
BaseSQLMetadataExtractionActivities with incremental extraction capabilities:

- Marker management (fetch/update)
- Current state management (read/write)
- Incremental column extraction (batched, parallel)
- Ancestral column merging

Usage:
    class OracleActivities(IncrementalSQLMetadataExtractionActivities):
        sql_client_class = OracleClient
        
        # Full extraction queries
        fetch_table_sql = queries.get("EXTRACT_TABLE")
        fetch_column_sql = queries.get("EXTRACT_COLUMN")
        
        # Incremental extraction queries
        incremental_table_sql = queries.get("EXTRACT_TABLE_INCREMENTAL")
        incremental_column_sql = queries.get("EXTRACT_COLUMN_INCREMENTAL")
        
        def resolve_database_placeholders(self, sql: str, workflow_args: Dict) -> str:
            return resolve_oracle_placeholders(sql, workflow_args)
"""
from __future__ import annotations

import os
from abc import abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional, Set, cast

from temporalio import activity

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import auto_heartbeater
from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
    BaseSQLMetadataExtractionActivitiesState,
)
from application_sdk.io.json import JsonFileWriter
from application_sdk.io.parquet import ParquetFileReader
from application_sdk.io import DataframeType
from application_sdk.io.utils import is_empty_dataframe
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.statestore import StateStore, StateType

# Incremental models and utilities
from application_sdk.common.incremental.models import IncrementalWorkflowArgs
from application_sdk.common.incremental.helpers import (
    get_persistent_s3_prefix,
    get_persistent_artifacts_path,
    is_incremental_run,
)

# State management helpers (modular helpers for read/write current state)
from application_sdk.common.incremental.state.state_reader import download_current_state
from application_sdk.common.incremental.state.state_writer import (
    download_transformed_data,
    prepare_previous_state,
    cleanup_previous_state,
    create_current_state_snapshot,
)
from application_sdk.common.incremental.state.marker import (
    fetch_marker_from_storage,
    persist_marker_to_storage,
    create_next_marker,
)

logger = get_logger(__name__)
activity.logger = logger


class IncrementalSQLMetadataExtractionActivities(BaseSQLMetadataExtractionActivities):
    """Activities for incremental SQL metadata extraction.

    This class extends BaseSQLMetadataExtractionActivities with incremental
    extraction capabilities:

    - fetch_incremental_marker: Download and read marker from S3
    - update_incremental_marker: Persist next marker timestamp to S3
    - read_current_state: Download current-state folder from S3
    - write_current_state: Create current-state snapshot with ancestral merge
    - prepare_column_extraction_queries: Prepare batched column queries
    - execute_single_column_batch: Execute a single column extraction batch

    Subclasses must implement:
    - resolve_database_placeholders: Replace database-specific SQL placeholders

    Subclasses should set class attributes:
    - incremental_table_sql: SQL query for incremental table extraction
    - incremental_column_sql: SQL query for incremental column extraction
    """

    # Incremental SQL queries (to be set by subclasses)
    incremental_table_sql: Optional[str] = None
    incremental_column_sql: Optional[str] = None

    @abstractmethod
    def resolve_database_placeholders(
        self, sql: str, workflow_args: Dict[str, Any]
    ) -> str:
        """Replace database-specific placeholders in SQL templates.

        This method should handle database-specific placeholders like:
        - {system_schema}: System schema name (e.g., SYS for Oracle)
        - {marker_timestamp}: Timestamp for incremental filtering

        Args:
            sql: SQL template with placeholders
            workflow_args: Dictionary containing workflow configuration

        Returns:
            SQL string with placeholders resolved
        """
        raise NotImplementedError(
            "Subclasses must implement resolve_database_placeholders"
        )

    def get_backfill_tables(
        self,
        current_transformed_dir: Path,
        previous_current_state_dir: Optional[Path],
    ) -> Optional[Set[str]]:
        """Detect tables that need backfill (optional override).

        Backfill tables are those that exist in current extraction but
        were not in the previous state (e.g., due to filter changes).

        Override this method to implement database-specific backfill detection.

        Args:
            current_transformed_dir: Path to current run's transformed output
            previous_current_state_dir: Path to previous run's current-state

        Returns:
            Set of table qualified names needing backfill, or None
        """
        return None

    @activity.defn
    @auto_heartbeater
    async def fetch_tables(
        self, workflow_args: Dict[str, Any]
    ) -> Optional[ActivityStatistics]:
        """Fetch tables using appropriate SQL based on incremental mode.

        Uses incremental_table_sql when all incremental prerequisites are met,
        otherwise falls back to fetch_table_sql.

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Statistics about the extracted tables, or None if extraction failed.
        """
        args = IncrementalWorkflowArgs.model_validate(workflow_args)
        inc_enabled = is_incremental_run(workflow_args)

        if inc_enabled and self.incremental_table_sql:
            resolved_sql = self.resolve_database_placeholders(
                self.incremental_table_sql, workflow_args
            )
            self.fetch_table_sql = resolved_sql
            logger.info(
                f"Using incremental table SQL (marker: {args.metadata.marker_timestamp})"
            )
        else:
            if self.fetch_table_sql:
                resolved_sql = self.resolve_database_placeholders(
                    self.fetch_table_sql, workflow_args
                )
                self.fetch_table_sql = resolved_sql
            logger.info("Using full table extraction SQL")

        return await super().fetch_tables(workflow_args)

    @activity.defn
    @auto_heartbeater
    async def fetch_columns(
        self, workflow_args: Dict[str, Any]
    ) -> Optional[ActivityStatistics]:
        """Fetch columns - skipped for incremental runs (handled separately).

        In incremental mode, column extraction is handled by
        prepare_column_extraction_queries and execute_single_column_batch.

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Statistics about the extracted columns, or None if skipped.
        """
        inc_enabled = is_incremental_run(workflow_args)

        if inc_enabled:
            logger.info("Skipping generic column extraction for incremental run")
            return None

        logger.info("Using full column extraction SQL")
        if self.fetch_column_sql:
            resolved_sql = self.resolve_database_placeholders(
                self.fetch_column_sql, workflow_args
            )
            self.fetch_column_sql = resolved_sql

        return await super().fetch_columns(workflow_args)

    @activity.defn
    @auto_heartbeater
    async def fetch_incremental_marker(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Download and read marker from S3 if incremental extraction is enabled.

        S3 Path: persistent-artifacts/apps/{application_name}/connection/{connection_id}/marker.txt

        The marker is used to filter extracted data to only include changes since
        the last successful extraction. Uses helper functions from state.marker module.

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Updated workflow_args with marker_timestamp and next_marker_timestamp.
        """
        args = IncrementalWorkflowArgs.model_validate(workflow_args)
        logger.info(
            f"Fetching incremental marker (enabled: {args.metadata.incremental_extraction})"
        )

        if not args.metadata.incremental_extraction:
            logger.info("Incremental extraction disabled - skipping marker fetch")
            return args.model_dump(by_alias=True, exclude_none=True)

        # Fetch marker and create next marker using helper
        marker, next_marker = await fetch_marker_from_storage(
            workflow_args=workflow_args,
            prepone_enabled=args.metadata.prepone_marker_timestamp,
            prepone_hours=args.metadata.prepone_marker_hours,
        )

        args.metadata.marker_timestamp = marker
        args.metadata.next_marker_timestamp = next_marker

        return args.model_dump(by_alias=True, exclude_none=True)

    @activity.defn
    @auto_heartbeater
    async def update_incremental_marker(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Persist next marker timestamp to S3 via ObjectStore.

        S3 Path: persistent-artifacts/apps/{application_name}/connection/{connection_id}/marker.txt

        This marker will be used by the next incremental run to filter data.
        Uses helper function from state.marker module.

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Dictionary with marker write status and details.
        """
        args = IncrementalWorkflowArgs.model_validate(workflow_args)

        if not args.metadata.incremental_extraction:
            logger.info("Incremental extraction disabled - skipping marker update")
            return {"marker_written": False, "reason": "incremental_disabled"}

        next_marker_value = args.metadata.next_marker_timestamp
        if not next_marker_value:
            raise ValueError(
                "next_marker_timestamp not found in workflow args. "
                "This should have been set by fetch_incremental_marker activity."
            )

        # Persist marker using helper function
        return await persist_marker_to_storage(
            workflow_args=workflow_args,
            marker_value=next_marker_value,
        )

    @activity.defn
    @auto_heartbeater
    async def read_current_state(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Download current-state folder from S3.

        Downloads the previous run's current-state snapshot, which is used for:
        1. Comparing with current extraction to detect changed tables
        2. Providing ancestral column data for unchanged tables

        S3 Path: persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Updated workflow_args with current_state metadata including:
            - current_state_path: Local path to downloaded state
            - current_state_available: Whether state was found
            - current_state_s3_prefix: S3 location of state
            - current_state_json_count: Number of JSON files

        Note:
            Uses state_reader.download_current_state() helper for the download logic.
        """
        try:
            args = IncrementalWorkflowArgs.model_validate(workflow_args)

            # Use helper function for download logic
            current_state_dir, current_state_s3_prefix, exists, json_count = (
                await download_current_state(workflow_args)
            )

            # Update args with downloaded state metadata
            args.metadata.current_state_path = str(current_state_dir)
            args.metadata.current_state_available = exists
            args.metadata.current_state_s3_prefix = current_state_s3_prefix
            args.metadata.current_state_json_count = json_count

            return args.model_dump(by_alias=True, exclude_none=True)
        except Exception as e:
            logger.error(f"Failed to read current-state: {e}")
            raise

    @activity.defn
    @auto_heartbeater
    async def write_current_state(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create current-state snapshot with ancestral merge and upload to S3.

        This method implements ancestral state preservation for incremental extraction:
        1. Tables: Always use current transformed (authoritative for table state)
        2. Columns: Merge current (CREATED/UPDATED tables) with ancestral (NO CHANGE tables)
        3. Other entities (schema, database): Always use current transformed

        S3 Path: persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/

        Uses create_current_state_snapshot() helper for the complex orchestration logic.

        Args:
            workflow_args: Workflow arguments containing paths and configuration

        Returns:
            Updated workflow_args with current state metadata
        """
        workflow_id = workflow_args.get("workflow_id", "unknown")
        run_id = workflow_args.get("workflow_run_id", "unknown")
        args = IncrementalWorkflowArgs.model_validate(workflow_args)

        logger.info(
            f"Starting write_current_state with ancestral merge "
            f"(workflow: {workflow_id}, run: {run_id})"
        )

        try:
            # Step 1: Download current transformed files from S3
            output_path_str = str(workflow_args.get("output_path", "")).strip()
            transformed_dir = await download_transformed_data(output_path_str)

            # Step 2: Prepare paths
            s3_prefix = get_persistent_s3_prefix(workflow_args)
            current_state_dir = get_persistent_artifacts_path(
                workflow_args, "current-state"
            )

            # Step 3: Download previous state to a temporary location
            previous_state_dir = await prepare_previous_state(
                workflow_args=workflow_args,
                current_state_available=args.metadata.current_state_available,
                current_state_dir=current_state_dir,
            )

            # Step 4: Create snapshot using orchestration helper
            # This handles: table scope, directory prep, entity copy, column merge,
            # incremental diff creation, and S3 upload
            result = await create_current_state_snapshot(
                workflow_args=workflow_args,
                transformed_dir=transformed_dir,
                previous_state_dir=previous_state_dir,
                current_state_dir=current_state_dir,
                s3_prefix=s3_prefix,
                run_id=run_id,
                copy_workers=args.metadata.copy_workers,
                column_chunk_size=args.metadata.column_chunk_size,
                get_backfill_tables_fn=self.get_backfill_tables,
            )

            # Step 5: Clean up temporary previous state directory
            cleanup_previous_state(previous_state_dir)

            # Update metadata with results
            args.metadata.current_state_path = str(result.current_state_dir)
            args.metadata.current_state_files = result.total_files
            args.metadata.current_state_s3_prefix = result.current_state_s3_prefix
            args.metadata.incremental_diff_path = (
                str(result.incremental_diff_dir) if result.incremental_diff_dir else None
            )
            args.metadata.incremental_diff_s3_prefix = result.incremental_diff_s3_prefix
            args.metadata.incremental_diff_files = result.incremental_diff_files

            return args.model_dump(by_alias=True, exclude_none=True)

        except Exception as e:
            logger.error(f"Failed to write current-state: {e}")
            raise

    @activity.defn
    @auto_heartbeater
    async def prepare_column_extraction_queries(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Prepare batched column extraction queries for incremental and backfill tables.

        This is a base implementation that returns empty results.
        Subclasses should override this method to implement database-specific
        column query generation.

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Dictionary with query file paths and counts.
        """
        logger.info("Base prepare_column_extraction_queries - no queries generated")
        return {
            "queries": [],
            "total_batches": 0,
            "changed_tables": 0,
            "backfill_tables": 0,
            "total_tables": 0,
        }

    @activity.defn
    @auto_heartbeater
    async def execute_single_column_batch(
        self, batch_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a single column extraction batch.

        This activity is designed to be called in parallel from the workflow.
        Each invocation processes one SQL batch file.

        This is a base implementation that should be overridden by subclasses
        to implement database-specific batch execution.

        Args:
            batch_args: Dictionary containing batch_index, total_batches, and workflow args.

        Returns:
            Dictionary with batch execution results including record count.
        """
        batch_idx = batch_args.get("batch_index", 0)
        total_batches = batch_args.get("total_batches", 1)
        logger.info(f"Base execute_single_column_batch - batch {batch_idx + 1}/{total_batches}")
        return {
            "batch_index": batch_idx,
            "records": 0,
            "status": "base_implementation",
        }

    @activity.defn
    @auto_heartbeater
    async def transform_data(
        self,
        workflow_args: Dict[str, Any],
    ) -> ActivityStatistics:
        """Transform extracted data to Atlas format.

        This override of the base transform_data is necessary because incremental
        extraction uses a different directory structure for raw data:

        - When file_names is provided (incremental column batches):
          Read from {output_path}/raw/ with specific file names
          e.g., raw/column-batch-0.parquet, raw/column-batch-1.parquet

        - When file_names is None (regular entity transform):
          Read from {output_path}/raw/{typename}/ directory
          e.g., raw/table/, raw/schema/, raw/database/

        Args:
            workflow_args: Dictionary containing workflow configuration.
                - file_names: Optional list of specific parquet files to process.
                              None for full entity type transforms.
                - typename: Entity type being transformed (table, column, etc.)

        Returns:
            Statistics about the transformed data.
        """
        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        output_prefix, output_path, typename, workflow_id, workflow_run_id = (
            self._validate_output_args(workflow_args)
        )

        # Determine input path based on whether specific files are requested:
        # - file_names provided: Read from raw/ root (for incremental column batches)
        # - file_names is None: Read from raw/{typename}/ subdirectory (standard flow)
        file_names = workflow_args.get("file_names")
        if file_names:
            raw_input_path = os.path.join(output_path, "raw")
        else:
            raw_input_path = os.path.join(output_path, "raw", typename)
        logger.info(f"Reading raw data from: {raw_input_path} (typename={typename})")

        raw_input = ParquetFileReader(
            path=raw_input_path,
            file_names=file_names,
            dataframe_type=DataframeType.daft,
        )
        raw_input = raw_input.read_batches()

        transformed_output = JsonFileWriter(
            path=os.path.join(output_path, "transformed"),
            typename=typename,
            chunk_start=workflow_args.get("chunk_start"),
            dataframe_type=DataframeType.daft,
        )

        if state.transformer:
            workflow_args["connection_name"] = workflow_args.get("connection", {}).get(
                "connection_name", None
            )
            workflow_args["connection_qualified_name"] = workflow_args.get(
                "connection", {}
            ).get("connection_qualified_name", None)

            async for dataframe in raw_input:
                if not is_empty_dataframe(dataframe):
                    logger.info(f"Processing dataframe: (typename={typename})")

                    transform_metadata = state.transformer.transform_metadata(
                        dataframe=dataframe, **workflow_args
                    )
                    await transformed_output.write(transform_metadata)

        return await transformed_output.close()

    @activity.defn
    @auto_heartbeater
    async def save_workflow_state(
        self, workflow_id: str, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Save workflow state to state store.

        Args:
            workflow_id: Unique identifier for the workflow.
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Result of state save operation.
        """
        try:
            return await StateStore.save_state_object(
                id=workflow_id, value=workflow_args, type=StateType.WORKFLOWS
            )
        except Exception as e:
            logger.error(f"Failed to save workflow state for {workflow_id}: {e}")
            raise
