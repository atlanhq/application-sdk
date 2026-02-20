"""Incremental SQL metadata extraction activities.

This module provides the IncrementalSQLMetadataExtractionActivities class that extends
BaseSQLMetadataExtractionActivities with incremental extraction capabilities:

- Marker management (fetch/update)
- Current state management (read/write)
- Incremental column extraction (batched, parallel)
- Ancestral column merging

The SDK provides generic orchestration (table analysis, S3 management, parallel
execution) while apps provide the SQL-building strategy via build_incremental_column_sql().

Usage:
    class ClickHouseActivities(IncrementalSQLMetadataExtractionActivities):
        sql_client_class = ClickHouseClient

        # Full extraction queries
        fetch_table_sql = queries.get("EXTRACT_TABLE")
        fetch_column_sql = queries.get("EXTRACT_COLUMN")

        # Incremental extraction queries
        incremental_table_sql = queries.get("EXTRACT_TABLE_INCREMENTAL")

        def build_incremental_column_sql(self, table_ids, workflow_args) -> str:
            # App builds the SQL query for these table_ids
            return self._build_where_in_query(table_ids, workflow_args)
"""

from __future__ import annotations

import json
import os
from abc import abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, cast

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

# Column extraction helpers (generic orchestration utilities)
from application_sdk.common.incremental.column_extraction import (
    get_backfill_tables,
    get_tables_needing_column_extraction,
)
from application_sdk.common.incremental.helpers import (
    download_s3_prefix_with_structure,
    get_persistent_artifacts_path,
    get_persistent_s3_prefix,
)
from application_sdk.common.incremental.marker import (
    fetch_marker_from_storage,
    persist_marker_to_storage,
)

# Incremental models and utilities
from application_sdk.common.incremental.models import IncrementalWorkflowArgs

# State management helpers (modular helpers for read/write current state)
from application_sdk.common.incremental.state.state_reader import download_current_state
from application_sdk.common.incremental.state.state_writer import (
    cleanup_previous_state,
    create_current_state_snapshot,
    download_transformed_data,
    prepare_previous_state,
)
from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
from application_sdk.io import DataframeType
from application_sdk.io.json import JsonFileWriter
from application_sdk.io.parquet import ParquetFileReader
from application_sdk.io.utils import is_empty_dataframe
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.objectstore import ObjectStore
from application_sdk.services.statestore import StateStore, StateType

logger = get_logger(__name__)
activity.logger = logger


def _ensure_batches_dir(workflow_args: Dict[str, Any]) -> Path:
    """Create and return the batches output directory for table_id JSON files.

    Args:
        workflow_args: Dictionary containing workflow configuration with output_path.

    Returns:
        Path to the batches directory.

    Raises:
        ValueError: If output_path is not provided.
    """
    output_path_str = str(workflow_args.get("output_path", "")).strip()

    if not output_path_str:
        raise ValueError("Cannot determine batches directory - no output_path provided")

    bdir = Path(output_path_str).joinpath("batches", "column-table-ids")
    bdir.mkdir(parents=True, exist_ok=True)
    return bdir


class IncrementalSQLMetadataExtractionActivities(BaseSQLMetadataExtractionActivities):
    """Activities for incremental SQL metadata extraction.

    This class extends BaseSQLMetadataExtractionActivities with incremental
    extraction capabilities including marker management, current state
    management, and batched parallel column extraction with ancestral merging.

    Subclasses must implement ``build_incremental_column_sql`` to build the
    SQL query string for a batch of table_ids. Subclasses may optionally
    override ``resolve_database_placeholders`` for database-specific SQL
    placeholders (default is no-op). Subclasses should set
    ``incremental_table_sql`` for incremental table extraction.

    The SDK's default TABLE and COLUMN YAML templates include
    ``incremental_state`` in customAttributes. This field is silently
    skipped during full extraction and flows through during incremental
    extraction.
    """

    # Incremental SQL queries (to be set by subclasses)
    incremental_table_sql: Optional[str] = None

    # Original SQL templates (saved on first access to prevent mutation across runs)
    _original_fetch_table_sql: Optional[str] = None
    _original_fetch_column_sql: Optional[str] = None

    def _resolve_common_placeholders(
        self, sql: str, workflow_args: Dict[str, Any]
    ) -> str:
        """Replace universal placeholders shared by all connectors.

        Currently handles:
        - {marker_timestamp}: Timestamp for incremental filtering

        This is called automatically by the SDK before resolve_database_placeholders
        and inside run_column_query. Apps should NOT override this method.

        Args:
            sql: SQL template with placeholders
            workflow_args: Dictionary containing workflow configuration

        Returns:
            SQL string with common placeholders resolved
        """
        args = IncrementalWorkflowArgs.model_validate(workflow_args)
        marker = args.metadata.marker_timestamp
        if marker:
            sql = sql.replace("{marker_timestamp}", marker)
        return sql

    def resolve_database_placeholders(
        self, sql: str, workflow_args: Dict[str, Any]
    ) -> str:
        """Replace database-specific placeholders in SQL templates.

        Override this method to handle database-specific placeholders like:
        - {system_schema}: System schema name (e.g., SYS for Oracle)

        Note: {marker_timestamp} is handled automatically by the SDK via
        _resolve_common_placeholders — apps do NOT need to handle it.

        The default implementation is a no-op (returns sql unchanged).
        Only override if your database has app-specific placeholders.

        Args:
            sql: SQL template with placeholders
            workflow_args: Dictionary containing workflow configuration

        Returns:
            SQL string with placeholders resolved
        """
        return sql

    @abstractmethod
    def build_incremental_column_sql(
        self,
        table_ids: List[str],
        workflow_args: Dict[str, Any],
    ) -> str:
        """Build the SQL query for incremental column extraction.

        This is the app-specific method where each database connector decides
        how to construct the column query for the given table_ids. For example:
        - Oracle: Build a CTE with FROM dual UNION ALL syntax
        - ClickHouse: Use a simple WHERE ... IN (...) clause
        - PostgreSQL: Use ANY(ARRAY[...]) syntax

        The SDK handles all orchestration (identifying tables, batching,
        parallel execution, S3 management, query execution). The app only
        needs to build the SQL string.

        Args:
            table_ids: List of table IDs (catalog.schema.table format)
            workflow_args: Dictionary containing workflow configuration

        Returns:
            Fully rendered SQL query string ready for execution
        """
        raise NotImplementedError(
            "Subclasses must implement build_incremental_column_sql"
        )

    async def execute_column_batch(
        self,
        table_ids: List[str],
        workflow_args: Dict[str, Any],
        batch_idx: int,
        total_batches: int,
    ) -> int:
        """Execute column extraction for a batch of table IDs.

        Builds the SQL query via the app-specific build_incremental_column_sql()
        and executes it using run_column_query(). This method is concrete in the
        SDK — apps only need to implement build_incremental_column_sql().

        Args:
            table_ids: List of table IDs (catalog.schema.table format)
            workflow_args: Dictionary containing workflow configuration
            batch_idx: Zero-based index of this batch
            total_batches: Total number of batches

        Returns:
            Number of column records extracted
        """
        sql = self.build_incremental_column_sql(table_ids, workflow_args)
        return await self.run_column_query(sql, workflow_args, batch_idx)

    async def run_column_query(
        self,
        sql_query: str,
        workflow_args: Dict[str, Any],
        batch_idx: int,
    ) -> int:
        """Execute a column extraction SQL query and return record count.

        This is a helper method for subclasses to use within their
        execute_column_batch implementation. It handles SQL client setup,
        query execution, and result counting.

        Args:
            sql_query: The fully rendered SQL query to execute.
            workflow_args: Dictionary containing workflow configuration.
            batch_idx: Zero-based batch index (used for output path).

        Returns:
            Number of records extracted.
        """
        # Resolve common placeholders (e.g., {marker_timestamp}) before execution
        sql_query = self._resolve_common_placeholders(sql_query, workflow_args)

        state = cast(
            BaseSQLMetadataExtractionActivitiesState,
            await self._get_state(workflow_args),
        )
        if not state.sql_client:
            raise ValueError("SQL client not initialized")

        args = IncrementalWorkflowArgs.model_validate(workflow_args)
        chunk_size = args.metadata.column_chunk_size

        batch_workflow_args = dict(workflow_args)
        batch_workflow_args["chunk_size"] = chunk_size

        result = await self.query_executor(
            sql_client=state.sql_client,
            sql_query=sql_query,
            workflow_args=batch_workflow_args,
            output_path=os.path.join(
                workflow_args.get("output_path", ""),
                f"raw/column/batch-{batch_idx}",
            ),
            typename="column",
        )

        batch_records = 0
        if result and hasattr(result, "total_record_count"):
            batch_records = result.total_record_count
        elif isinstance(result, dict) and "total_record_count" in result:
            batch_records = result["total_record_count"]

        return batch_records

    def get_backfill_tables(
        self,
        current_transformed_dir: Path,
        previous_current_state_dir: Optional[Path],
    ) -> Optional[Set[str]]:
        """Detect tables that need backfill.

        Uses DuckDB to compare current vs previous state and find tables
        that exist now but weren't in the previous state.

        This default implementation uses the generic get_backfill_tables helper.
        Subclasses can override for database-specific behavior if needed.

        Args:
            current_transformed_dir: Path to current run's transformed output
            previous_current_state_dir: Path to previous run's current-state

        Returns:
            Set of table qualified names needing backfill, or None
        """
        return get_backfill_tables(current_transformed_dir, previous_current_state_dir)

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

        # Save original SQL template on first access to prevent mutation across runs
        # (Temporal reuses activity instances, so self.fetch_table_sql must not be
        #  permanently overwritten with a resolved version from a previous run)
        if self._original_fetch_table_sql is None:
            self._original_fetch_table_sql = self.fetch_table_sql

        if args.is_incremental_ready() and self.incremental_table_sql:
            resolved_sql = self._resolve_common_placeholders(
                self.incremental_table_sql, workflow_args
            )
            resolved_sql = self.resolve_database_placeholders(
                resolved_sql, workflow_args
            )
            self.fetch_table_sql = resolved_sql
            logger.info(
                f"Using incremental table SQL (marker: {args.metadata.marker_timestamp})"
            )
        else:
            base_sql = self._original_fetch_table_sql
            if base_sql:
                resolved_sql = self._resolve_common_placeholders(
                    base_sql, workflow_args
                )
                resolved_sql = self.resolve_database_placeholders(
                    resolved_sql, workflow_args
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
        args = IncrementalWorkflowArgs.model_validate(workflow_args)

        if args.is_incremental_ready():
            logger.info("Skipping generic column extraction for incremental run")
            return None

        # Save original SQL template on first access (same reason as fetch_tables)
        if self._original_fetch_column_sql is None:
            self._original_fetch_column_sql = self.fetch_column_sql

        logger.info("Using full column extraction SQL")
        base_sql = self._original_fetch_column_sql
        if base_sql:
            resolved_sql = self._resolve_common_placeholders(base_sql, workflow_args)
            resolved_sql = self.resolve_database_placeholders(
                resolved_sql, workflow_args
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
    async def read_current_state(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """Download current-state folder from S3.

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Updated workflow_args with current_state metadata.
        """
        try:
            args = IncrementalWorkflowArgs.model_validate(workflow_args)

            (
                current_state_dir,
                current_state_s3_prefix,
                exists,
                json_count,
            ) = await download_current_state(workflow_args)

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

        previous_state_dir = None
        try:
            output_path_str = str(workflow_args.get("output_path", "")).strip()
            transformed_dir = await download_transformed_data(output_path_str)

            s3_prefix = get_persistent_s3_prefix(workflow_args)
            current_state_dir = get_persistent_artifacts_path(
                workflow_args, "current-state"
            )

            previous_state_dir = await prepare_previous_state(
                workflow_args=workflow_args,
                current_state_available=args.metadata.current_state_available,
                current_state_dir=current_state_dir,
            )

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

            args.metadata.current_state_path = str(result.current_state_dir)
            args.metadata.current_state_files = result.total_files
            args.metadata.current_state_s3_prefix = result.current_state_s3_prefix
            args.metadata.incremental_diff_path = (
                str(result.incremental_diff_dir)
                if result.incremental_diff_dir
                else None
            )
            args.metadata.incremental_diff_s3_prefix = result.incremental_diff_s3_prefix
            args.metadata.incremental_diff_files = result.incremental_diff_files

            return args.model_dump(by_alias=True, exclude_none=True)

        except Exception as e:
            logger.error(f"Failed to write current-state: {e}")
            raise
        finally:
            # Always clean up temporary previous state directory, even on failure
            cleanup_previous_state(previous_state_dir)

    @activity.defn
    @auto_heartbeater
    async def prepare_column_extraction_queries(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Identify tables needing column extraction and batch their IDs.

        This generic implementation:
        1. Downloads transformed files from S3
        2. Downloads previous current-state for backfill comparison
        3. Uses DuckDB to detect backfill tables
        4. Uses Daft to identify tables needing column extraction
        5. Batches table_ids into JSON files and uploads to S3

        Apps do NOT need to override this method. The query strategy is
        handled entirely by execute_column_batch().

        Args:
            workflow_args: Dictionary containing workflow configuration.

        Returns:
            Dictionary with batch counts and table statistics.
        """
        args = IncrementalWorkflowArgs.model_validate(workflow_args)

        if not args.output_path:
            raise FileNotFoundError("No output_path provided in workflow_args")

        # Step 1: Download transformed files from S3
        transformed_local_path = os.path.join(args.output_path, "transformed")
        transformed_s3_prefix = get_object_store_prefix(transformed_local_path)

        transformed_dir = Path(transformed_local_path)
        transformed_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Downloading transformed files from S3: {transformed_s3_prefix}")
        await ObjectStore.download_prefix(
            source=transformed_s3_prefix,
            destination=str(transformed_dir),
            store_name=UPSTREAM_OBJECT_STORE_NAME,
        )

        batch_size = args.metadata.column_batch_size
        logger.info(f"Preparing column extraction batches (batch_size={batch_size})")

        # Step 2: Download previous current-state from S3 for backfill comparison
        current_state_available = args.metadata.current_state_available
        previous_current_state_dir = None

        if current_state_available:
            s3_prefix = get_persistent_s3_prefix(workflow_args)
            current_state_s3_prefix = f"{s3_prefix}/current-state"
            previous_current_state_dir = get_persistent_artifacts_path(
                workflow_args, "current-state"
            )

            previous_current_state_dir.mkdir(parents=True, exist_ok=True)

            table_dir = previous_current_state_dir.joinpath("table")
            has_table_files = table_dir.exists() and any(table_dir.glob("*.json"))

            if not has_table_files:
                logger.info(
                    f"Downloading current-state from S3 for backfill comparison: "
                    f"{current_state_s3_prefix}"
                )
                try:
                    await download_s3_prefix_with_structure(
                        s3_prefix=current_state_s3_prefix,
                        local_destination=previous_current_state_dir,
                    )
                    logger.info(
                        f"Current-state downloaded to: {previous_current_state_dir}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to download current-state for backfill: {e}"
                    )
                    previous_current_state_dir = None
            else:
                table_file_count = len(list(table_dir.glob("*.json")))
                logger.info(
                    f"Previous current-state already present at: "
                    f"{previous_current_state_dir} (table files: {table_file_count})"
                )

        # Step 3: Find backfill tables using DuckDB
        logger.info("Analyzing table state to identify backfill candidates...")
        backfill_qns = self.get_backfill_tables(
            transformed_dir, previous_current_state_dir
        )
        backfill_count_for_log = len(backfill_qns) if backfill_qns else 0
        logger.info(f"Found {backfill_count_for_log} tables needing backfill")

        # Step 4: Get tables needing column extraction using Daft
        filtered_df, changed_count, backfill_count, no_change_count = (
            get_tables_needing_column_extraction(transformed_dir, backfill_qns)
        )

        total_tables = changed_count + backfill_count
        if total_tables == 0:
            logger.info("No tables need column extraction")
            return {
                "total_batches": 0,
                "changed_tables": 0,
                "backfill_tables": 0,
                "total_tables": 0,
            }

        logger.info(f"Batching {total_tables} tables into groups of {batch_size}")

        # Step 5: Batch table_ids into JSON files
        output_dir = _ensure_batches_dir(workflow_args)
        batch_idx = 0
        total_tables_batched = 0
        current_batch: List[str] = []

        for row in filtered_df.select("table_id").iter_rows():
            current_batch.append(row["table_id"])

            if len(current_batch) >= batch_size:
                batch_file = output_dir / f"batch-{batch_idx}.json"
                batch_file.write_text(json.dumps(current_batch), encoding="utf-8")
                batch_idx += 1
                total_tables_batched += len(current_batch)
                current_batch = []

        # Write remaining tables in final batch
        if current_batch:
            batch_file = output_dir / f"batch-{batch_idx}.json"
            batch_file.write_text(json.dumps(current_batch), encoding="utf-8")
            batch_idx += 1
            total_tables_batched += len(current_batch)

        logger.info(
            f"Created {batch_idx} batch files for {total_tables_batched} tables "
            f"in directory: {output_dir}"
        )

        # Step 6: Upload batch files to S3 for multi-worker support
        batches_s3_prefix = get_object_store_prefix(str(output_dir))
        logger.info(f"Uploading batch files to S3: {batches_s3_prefix}")
        await ObjectStore.upload_prefix(
            source=str(output_dir),
            destination=batches_s3_prefix,
            store_name=UPSTREAM_OBJECT_STORE_NAME,
            retain_local_copy=True,
        )

        return {
            "batches_s3_prefix": batches_s3_prefix,
            "batches_local_dir": str(output_dir),
            "changed_tables": changed_count,
            "backfill_tables": backfill_count,
            "total_tables": total_tables,
            "batch_size": batch_size,
            "total_batches": batch_idx,
        }

    @activity.defn
    @auto_heartbeater
    async def execute_single_column_batch(
        self, batch_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a single column extraction batch.

        This activity is called in parallel from the workflow. It downloads
        the batch's table_ids JSON file from S3 and delegates to the
        app-specific execute_column_batch() method.

        Args:
            batch_args: Dictionary containing batch_index, total_batches, and workflow args.

        Returns:
            Dictionary with batch execution results including record count.
        """
        batch_idx = batch_args.get("batch_index", 0)
        total_batches = batch_args.get("total_batches", 1)

        # Get batches directory path
        bdir = _ensure_batches_dir(batch_args)

        # Download only the specific batch file needed
        batch_filename = f"batch-{batch_idx}.json"
        batch_file = bdir / batch_filename
        batches_s3_prefix = get_object_store_prefix(str(bdir))

        await ObjectStore.download_file(
            source=f"{batches_s3_prefix}/{batch_filename}",
            destination=str(batch_file),
            store_name=UPSTREAM_OBJECT_STORE_NAME,
        )

        if not batch_file.exists():
            logger.warning(f"Batch file not found: {batch_file}")
            return {"batch_index": batch_idx, "records": 0, "status": "not_found"}

        # Read table_ids from JSON
        table_ids = json.loads(batch_file.read_text(encoding="utf-8"))

        logger.info(
            f"Executing batch {batch_idx + 1}/{total_batches}: {len(table_ids)} tables"
        )

        # Delegate to app-specific implementation
        batch_records = await self.execute_column_batch(
            table_ids=table_ids,
            workflow_args=batch_args,
            batch_idx=batch_idx,
            total_batches=total_batches,
        )

        logger.info(
            f"Batch {batch_idx + 1}/{total_batches} complete: {batch_records} records"
        )

        args = IncrementalWorkflowArgs.model_validate(batch_args)

        return {
            "batch_index": batch_idx,
            "records": batch_records,
            "status": "success",
            "chunk_size": args.metadata.column_chunk_size,
        }

    @activity.defn
    @auto_heartbeater
    async def transform_data(
        self,
        workflow_args: Dict[str, Any],
    ) -> ActivityStatistics:
        """Transform extracted data to Atlas format.

        This override is necessary because incremental extraction uses a different
        directory structure for raw data:
        - file_names provided: Read from raw/ root (incremental column batches)
        - file_names is None: Read from raw/{typename}/ subdirectory (standard)

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
                        dataframe=dataframe,  # type: ignore
                        **workflow_args,
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
