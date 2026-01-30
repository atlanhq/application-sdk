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
import shutil
from abc import abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set, cast

from temporalio import activity

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import (
    auto_heartbeater,
    get_object_store_prefix,
)
from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
    BaseSQLMetadataExtractionActivitiesState,
)
from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
from application_sdk.io.json import JsonFileWriter
from application_sdk.io.parquet import ParquetFileReader
from application_sdk.io import DataframeType
from application_sdk.io.utils import is_empty_dataframe
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore
from application_sdk.services.statestore import StateStore, StateType

# Incremental utilities
from application_sdk.common.incremental.models import (
    EntityType,
    IncrementalWorkflowArgs,
)
from application_sdk.common.incremental.constants import (
    MARKER_TIMESTAMP_FORMAT,
    INCREMENTAL_DIFF_SUBPATH_TEMPLATE,
)
from application_sdk.common.incremental.helpers import (
    get_persistent_s3_prefix,
    get_persistent_artifacts_path,
    normalize_marker_timestamp,
    prepone_marker_timestamp,
    download_marker_from_s3,
    is_incremental_run,
    download_s3_prefix_with_structure,
    count_json_files_recursive,
    copy_directory_parallel,
)
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
)
from application_sdk.common.incremental.table_scope import (
    get_current_table_scope,
    get_scope_length,
    close_scope,
)
from application_sdk.common.incremental.ancestral_merge import merge_ancestral_columns
from application_sdk.common.incremental.incremental_diff import create_incremental_diff

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

        # Always set next_marker for update_incremental_marker to use
        next_marker = datetime.now(timezone.utc).strftime(MARKER_TIMESTAMP_FORMAT)
        args.metadata.next_marker_timestamp = next_marker

        # Try workflow args first, then S3
        marker = args.metadata.marker_timestamp
        if marker:
            logger.info(f"Using marker from workflow args: {marker}")
        else:
            marker = await download_marker_from_s3(workflow_args)

        if not marker:
            logger.info(f"No marker found - full extraction (next_marker={next_marker})")
            return args.model_dump(by_alias=True, exclude_none=True)

        # Normalize marker timestamp
        normalized_marker = normalize_marker_timestamp(marker)

        # Apply preponing if enabled (moves marker back to catch edge cases)
        if (
            args.metadata.prepone_marker_timestamp
            and args.metadata.prepone_marker_hours > 0
        ):
            adjusted_marker = prepone_marker_timestamp(
                normalized_marker, args.metadata.prepone_marker_hours
            )
            args.metadata.marker_timestamp = adjusted_marker
            logger.info(
                f"Incremental extraction: original_marker={normalized_marker}, "
                f"adjusted_marker={adjusted_marker} "
                f"(preponed by {args.metadata.prepone_marker_hours}h), "
                f"next={next_marker}"
            )
        else:
            args.metadata.marker_timestamp = normalized_marker
            logger.info(
                f"Incremental extraction: marker={normalized_marker}, next={next_marker}"
            )

        return args.model_dump(by_alias=True, exclude_none=True)

    @activity.defn
    @auto_heartbeater
    async def update_incremental_marker(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Persist next marker timestamp to S3 via ObjectStore.

        S3 Path: persistent-artifacts/apps/{application_name}/connection/{connection_id}/marker.txt

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Dictionary with marker write status and details.
        """
        args = IncrementalWorkflowArgs.model_validate(workflow_args)

        should_write_marker = args.metadata.incremental_extraction

        if not should_write_marker:
            logger.info("Incremental extraction disabled - skipping marker update")
            return {"marker_written": False, "reason": "incremental_disabled"}

        next_marker_value = args.metadata.next_marker_timestamp
        if not next_marker_value:
            raise ValueError(
                "next_marker_timestamp not found in workflow args. "
                "This should have been set by fetch_incremental_marker activity."
            )

        s3_prefix = get_persistent_s3_prefix(workflow_args)
        marker_s3_key = f"{s3_prefix}/marker.txt"
        local_marker_path = get_persistent_artifacts_path(workflow_args, "marker.txt")

        # Ensure local directory exists
        local_marker_path.parent.mkdir(parents=True, exist_ok=True)

        # Write marker to local file
        logger.info(f"Writing marker to local file: {local_marker_path}")
        local_marker_path.write_text(next_marker_value, encoding="utf-8")

        # Upload marker to S3
        logger.info(f"Uploading marker to S3: {marker_s3_key}")
        try:
            await ObjectStore.upload_file(
                source=str(local_marker_path),
                destination=marker_s3_key,
                store_name=UPSTREAM_OBJECT_STORE_NAME,
                retain_local_copy=True,
            )
            logger.info(f"Marker uploaded to S3: {marker_s3_key} → {next_marker_value}")
        except Exception as e:
            logger.error(f"Failed to upload marker to S3: {e}")
            raise

        return {
            "marker_written": True,
            "marker_timestamp": next_marker_value,
            "local_path": str(local_marker_path),
            "s3_key": marker_s3_key,
        }

    @activity.defn
    @auto_heartbeater
    async def read_current_state(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Download current-state folder from S3.

        S3 Path: persistent-artifacts/apps/{application_name}/connection/{connection_id}/current-state/

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Updated workflow_args with current_state metadata.
        """
        try:
            args = IncrementalWorkflowArgs.model_validate(workflow_args)

            s3_prefix = get_persistent_s3_prefix(workflow_args)
            current_state_s3_prefix = f"{s3_prefix}/current-state"
            current_state_dir = get_persistent_artifacts_path(
                workflow_args, "current-state"
            )

            # Ensure local directory exists
            current_state_dir.mkdir(parents=True, exist_ok=True)

            logger.info(
                f"Downloading current-state folder from S3: {current_state_s3_prefix}"
            )

            exists = False
            json_count = 0

            try:
                await ObjectStore.download_prefix(
                    source=current_state_s3_prefix,
                    destination=str(current_state_dir),
                    store_name=UPSTREAM_OBJECT_STORE_NAME,
                )

                json_count = count_json_files_recursive(current_state_dir)
                exists = json_count > 0

                if exists:
                    logger.info(
                        f"Current-state downloaded with {json_count} JSON files"
                    )
                else:
                    logger.info("Current-state downloaded but empty (no JSON files)")
            except Exception as e:
                # First run - current-state doesn't exist in S3 yet
                logger.info(f"Current-state not found in S3 (first run): {e}")

            if not exists:
                logger.info("Current-state not available (first run or empty)")

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

        Args:
            workflow_args: Workflow arguments containing paths and configuration

        Returns:
            Updated workflow_args with current state metadata
        """
        workflow_id = workflow_args.get("workflow_id", "unknown")
        run_id = workflow_args.get("workflow_run_id", "unknown")
        connection_qn = workflow_args.get("connection", {}).get(
            "connection_qualified_name", "unknown"
        )
        args = IncrementalWorkflowArgs.model_validate(workflow_args)

        logger.info(
            f"Starting write_current_state with ancestral merge "
            f"(workflow: {workflow_id}, run: {run_id})"
        )

        try:
            output_path_str = str(workflow_args.get("output_path", "")).strip()
            if not output_path_str:
                raise FileNotFoundError("No output_path provided in workflow_args")

            # Step 1: Download current transformed files from S3
            transformed_local_path = os.path.join(output_path_str, "transformed")
            transformed_s3_prefix = get_object_store_prefix(transformed_local_path)

            logger.info(
                f"Downloading transformed files from S3: {transformed_s3_prefix}"
            )

            # Ensure local directory exists before download
            transformed_dir = Path(transformed_local_path)
            transformed_dir.mkdir(parents=True, exist_ok=True)

            await ObjectStore.download_prefix(
                source=transformed_s3_prefix,
                destination=str(transformed_dir),
                store_name=UPSTREAM_OBJECT_STORE_NAME,
            )

            # Step 2: Prepare current-state directory
            s3_prefix = get_persistent_s3_prefix(workflow_args)
            current_state_s3_prefix = f"{s3_prefix}/current-state"
            current_state_dir = get_persistent_artifacts_path(
                workflow_args, "current-state"
            )

            # Step 3: Download previous state to a temporary location
            previous_state_dir = None
            if (
                args.metadata.current_state_available
                and args.metadata.current_state_path
            ):
                previous_state_temp_dir = current_state_dir.parent.joinpath(
                    f"{current_state_dir.name}.previous"
                )

                # Clean up any existing temp directory from previous failed runs
                if previous_state_temp_dir.exists():
                    shutil.rmtree(previous_state_temp_dir)
                previous_state_temp_dir.mkdir(parents=True, exist_ok=True)

                # Download previous state from S3 to temporary location
                logger.info(
                    f"Downloading previous state from S3: {current_state_s3_prefix}"
                )
                try:
                    await download_s3_prefix_with_structure(
                        s3_prefix=current_state_s3_prefix,
                        local_destination=previous_state_temp_dir,
                    )
                    previous_state_dir = previous_state_temp_dir
                    logger.info(
                        f"Previous state downloaded to temporary location: "
                        f"{previous_state_temp_dir}"
                    )
                except Exception as e:
                    logger.error(f"Failed to download previous state: {e}")
                    if previous_state_temp_dir.exists():
                        shutil.rmtree(previous_state_temp_dir)
                    raise

            # Step 4: Create DuckDB connection for all operations
            table_scope = None

            with DuckDBConnectionManager() as conn_manager:
                conn = conn_manager.connection

                try:
                    # Step 5: Get table scope (qualified names and incremental states)
                    table_scope = get_current_table_scope(transformed_dir, conn=conn)
                    if not table_scope or get_scope_length(table_scope) == 0:
                        raise FileNotFoundError(
                            f"No tables found in transformed output: {transformed_dir}. "
                            "Cannot create current state without table metadata."
                        )

                    logger.info(
                        f"Current-state path: {current_state_dir} "
                        f"(connection: {connection_qn})"
                    )

                    # Step 6: Clear and recreate current-state directory
                    if current_state_dir.exists():
                        shutil.rmtree(current_state_dir)
                    current_state_dir.mkdir(parents=True, exist_ok=True)

                    # Step 7: Copy non-column entities from current transformed
                    for entity_type in [
                        EntityType.TABLE,
                        EntityType.SCHEMA,
                        EntityType.DATABASE,
                    ]:
                        entity_dir = transformed_dir.joinpath(entity_type.value)
                        if entity_dir.exists():
                            dest_dir = current_state_dir.joinpath(entity_type.value)
                            count = copy_directory_parallel(
                                entity_dir, dest_dir, max_workers=args.metadata.copy_workers
                            )
                            logger.info(
                                f"Copied {count} {entity_type.value} files to current state"
                            )

                    # Step 8: Merge columns (current + ancestral for NO CHANGE tables)
                    merge_result, tables_with_columns = merge_ancestral_columns(
                        current_transformed_dir=transformed_dir,
                        previous_state_dir=previous_state_dir,
                        new_state_dir=current_state_dir,
                        table_scope=table_scope,
                        column_chunk_size=args.metadata.column_chunk_size,
                        conn=conn,
                    )

                    # Update table_scope with extracted columns info (for logging only)
                    table_scope.tables_with_extracted_columns = tables_with_columns

                    total_files = count_json_files_recursive(current_state_dir)

                    logger.info(
                        f"Current-state merge complete: "
                        f"tables={get_scope_length(table_scope)}, "
                        f"columns={merge_result.columns_total} "
                        f"(current={merge_result.columns_from_current}, "
                        f"ancestral={merge_result.columns_from_ancestral}), "
                        f"excluded="
                        f"{merge_result.excluded_already_extracted + merge_result.excluded_table_removed}, "
                        f"total_files={total_files}"
                    )

                    # Step 9: Create incremental-diff (only changed assets from this run)
                    diff_result = None
                    incremental_diff_dir = None
                    incremental_diff_s3_prefix = None

                    if previous_state_dir and previous_state_dir.exists():
                        incremental_diff_subpath = (
                            INCREMENTAL_DIFF_SUBPATH_TEMPLATE.format(run_id=run_id)
                        )
                        incremental_diff_dir = get_persistent_artifacts_path(
                            workflow_args, incremental_diff_subpath
                        )
                        incremental_diff_s3_prefix = (
                            f"{s3_prefix}/{incremental_diff_subpath}"
                        )

                        # Clear and recreate incremental-diff directory
                        if incremental_diff_dir.exists():
                            shutil.rmtree(incremental_diff_dir)

                        diff_result = create_incremental_diff(
                            transformed_dir=transformed_dir,
                            incremental_diff_dir=incremental_diff_dir,
                            table_scope=table_scope,
                            previous_state_dir=previous_state_dir,
                            conn=conn,
                            copy_workers=args.metadata.copy_workers,
                            get_backfill_tables_fn=self.get_backfill_tables,
                        )

                        # Upload incremental-diff to S3 (persistent per-run)
                        await ObjectStore.upload_prefix(
                            source=str(incremental_diff_dir),
                            destination=incremental_diff_s3_prefix,
                            store_name=UPSTREAM_OBJECT_STORE_NAME,
                        )
                        logger.info(
                            f"Incremental-diff uploaded to S3: {incremental_diff_s3_prefix}"
                        )
                    else:
                        logger.info(
                            "Skipping incremental-diff creation "
                            "(first run - no previous state to diff against)"
                        )

                finally:
                    # Close the TableScope's disk-backed stores
                    if table_scope:
                        close_scope(table_scope)

            # Step 10: Clean up temporary previous state directory
            if previous_state_dir and previous_state_dir.exists():
                try:
                    shutil.rmtree(previous_state_dir)
                    logger.info(
                        f"Cleaned up temporary previous state directory: "
                        f"{previous_state_dir}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to clean up temporary previous state directory: {e}"
                    )

            # Step 11: Upload current-state to S3 (persistent)
            await ObjectStore.upload_prefix(
                source=str(current_state_dir),
                destination=current_state_s3_prefix,
                store_name=UPSTREAM_OBJECT_STORE_NAME,
            )
            logger.info(f"Current-state uploaded to S3: {current_state_s3_prefix}")

            args.metadata.current_state_path = str(current_state_dir)
            args.metadata.current_state_files = total_files
            args.metadata.current_state_s3_prefix = current_state_s3_prefix
            if diff_result:
                args.metadata.incremental_diff_path = (
                    str(incremental_diff_dir) if incremental_diff_dir else None
                )
                args.metadata.incremental_diff_s3_prefix = incremental_diff_s3_prefix
                args.metadata.incremental_diff_files = diff_result.total_files
            else:
                args.metadata.incremental_diff_path = None
                args.metadata.incremental_diff_s3_prefix = None
                args.metadata.incremental_diff_files = 0

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

        Reads from {output_path}/raw/{typename} directory when typename is provided,
        or {output_path}/raw when file_names filtering is used.

        Args:
            workflow_args: Dictionary containing workflow configuration.

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
