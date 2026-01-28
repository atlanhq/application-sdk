"""Incremental metadata extraction activities mixin.

This module provides the IncrementalMetadataExtractionMixin class that can be
composed with BaseSQLMetadataExtractionActivities to add incremental extraction
capabilities.

Approach A: Composable Activities with SDK Workflow Helpers
- Apps compose activities: `class MyActivities(BaseSQLMetadataExtractionActivities, IncrementalMetadataExtractionMixin)`
- Provides incremental activities that can be selectively used
- Maximum flexibility - apps choose which activities to use
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, cast

from temporalio import activity

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.common.utils import auto_heartbeater, get_object_store_prefix
from application_sdk.common.incremental.ancestral_merge import merge_ancestral_columns
from application_sdk.common.incremental.constants import (
    INCREMENTAL_DIFF_SUBPATH_TEMPLATE,
    MARKER_TIMESTAMP_FORMAT,
    MAX_CONCURRENT_COLUMN_BATCHES,
)
from application_sdk.common.incremental.helpers import (
    download_marker_from_s3,
    download_s3_prefix_with_structure,
    ensure_runtime_queries_dir,
    get_backfill_tables,
    get_persistent_artifacts_path,
    get_persistent_s3_prefix,
    get_tables_needing_column_extraction,
    get_transformed_dir,
    normalize_marker_timestamp,
    prepone_marker_timestamp,
    count_json_files_recursive,
    copy_directory_parallel,
    write_batched_queries_from_df,
)
from application_sdk.common.incremental.incremental_diff import create_incremental_diff
from application_sdk.common.incremental.models import EntityType, TableScope, WorkflowMetadata
from application_sdk.common.incremental.storage.duckdb_utils import DuckDBConnectionManager
from application_sdk.common.incremental.table_scope import (
    close_scope,
    get_current_table_scope,
    get_scope_length,
)
from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
from application_sdk.inputs.parquet import ParquetInput
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.json import JsonOutput
from application_sdk.services.objectstore import ObjectStore

logger = get_logger(__name__)
activity.logger = logger


class IncrementalMetadataExtractionMixin:
    """Mixin providing incremental extraction activities.

    This mixin adds incremental extraction capabilities to SQL metadata extraction
    activities. Compose with BaseSQLMetadataExtractionActivities:

        class OracleActivities(BaseSQLMetadataExtractionActivities, IncrementalMetadataExtractionMixin):
            sql_client_class = OracleClient
            incremental_table_sql = queries.get("EXTRACT_TABLE_INCREMENTAL")
            incremental_column_sql = queries.get("EXTRACT_COLUMN_INCREMENTAL")

            def resolve_database_placeholders(self, sql: str, workflow_args: Dict) -> str:
                return resolve_oracle_placeholders(sql, workflow_args)

    Activities provided:
    - fetch_incremental_marker: Download marker from S3
    - update_incremental_marker: Upload marker to S3
    - read_current_state: Download previous state from S3
    - write_current_state: Create new state with ancestral merge
    - prepare_column_extraction_queries: Generate batched column queries
    - execute_single_column_batch: Execute one column extraction batch

    Class attributes (to be set by subclass):
    - incremental_table_sql: SQL for incremental table extraction
    - incremental_column_sql: SQL for incremental column extraction

    Methods to override:
    - resolve_database_placeholders: Database-specific placeholder resolution
    - render_column_sql: Optional custom SQL rendering (default uses generic SQL)
    """

    # SQL queries for incremental extraction (override in subclass)
    incremental_table_sql: str | None = None
    incremental_column_sql: str | None = None

    def resolve_database_placeholders(self, sql: str, workflow_args: Dict) -> str:
        """Resolve database-specific placeholders in SQL queries.

        Override this method in your activities class to handle database-specific
        placeholders like {system_schema}, {marker_timestamp}, etc.

        Args:
            sql: SQL query with placeholders
            workflow_args: Workflow arguments containing metadata

        Returns:
            SQL with placeholders resolved
        """
        raise NotImplementedError(
            "resolve_database_placeholders must be implemented by subclasses"
        )

    def render_column_sql(
        self, template_sql: str, table_ids: List[str], schema_name: str, marker: str
    ) -> str:
        """Render column extraction SQL with table filter CTE.

        Override for database-specific SQL syntax (e.g., Oracle's FROM dual).
        Default implementation uses generic SQL VALUES syntax.

        Args:
            template_sql: SQL template with --TABLE_FILTER_CTE-- placeholder
            table_ids: List of table IDs to extract columns for
            schema_name: Database schema name
            marker: Marker timestamp

        Returns:
            Rendered SQL query
        """
        from application_sdk.common.incremental.helpers import render_column_sql
        return render_column_sql(template_sql, table_ids, schema_name, marker)

    # =========================================================================
    # Marker Management Activities
    # =========================================================================

    @activity.defn
    @auto_heartbeater
    async def fetch_incremental_marker(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Download and read marker from S3 if incremental extraction is enabled.

        S3 Path: persistent-artifacts/apps/{app}/connection/{connection_id}/marker.txt

        Args:
            workflow_args: Workflow arguments

        Returns:
            Updated workflow_args with marker_timestamp and next_marker_timestamp
        """
        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))
        logger.info(f"Fetching incremental marker (enabled: {args.incremental_extraction})")

        if not args.incremental_extraction:
            logger.info("Incremental extraction disabled - skipping marker fetch")
            return workflow_args

        # Set next_marker for update_incremental_marker
        next_marker = datetime.now(timezone.utc).strftime(MARKER_TIMESTAMP_FORMAT)
        args.next_marker_timestamp = next_marker

        # Try workflow args first, then S3
        marker = args.marker_timestamp
        if marker:
            logger.info(f"Using marker from workflow args: {marker}")
        else:
            marker = await download_marker_from_s3(workflow_args)

        if not marker:
            logger.info(f"No marker found - full extraction (next_marker={next_marker})")
            workflow_args["metadata"] = args.model_dump(by_alias=True, exclude_none=True)
            return workflow_args

        # Normalize and optionally prepone
        normalized_marker = normalize_marker_timestamp(marker)

        if args.prepone_marker_timestamp and args.prepone_marker_hours > 0:
            adjusted_marker = prepone_marker_timestamp(
                normalized_marker, args.prepone_marker_hours
            )
            args.marker_timestamp = adjusted_marker
            logger.info(
                f"Incremental: original={normalized_marker}, adjusted={adjusted_marker} "
                f"(preponed by {args.prepone_marker_hours}h)"
            )
        else:
            args.marker_timestamp = normalized_marker
            logger.info(f"Incremental: marker={normalized_marker}, next={next_marker}")

        workflow_args["metadata"] = args.model_dump(by_alias=True, exclude_none=True)
        return workflow_args

    @activity.defn
    @auto_heartbeater
    async def update_incremental_marker(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Persist next marker timestamp to S3.

        S3 Path: persistent-artifacts/apps/{app}/connection/{connection_id}/marker.txt

        Args:
            workflow_args: Workflow arguments with next_marker_timestamp

        Returns:
            Result dict with marker_written status
        """
        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))

        if not args.incremental_extraction:
            logger.info("Incremental extraction disabled - skipping marker update")
            return {"marker_written": False, "reason": "incremental_disabled"}

        next_marker_value = args.next_marker_timestamp
        if not next_marker_value:
            raise ValueError(
                "next_marker_timestamp not found. Should have been set by fetch_incremental_marker."
            )

        s3_prefix = get_persistent_s3_prefix(workflow_args)
        marker_s3_key = f"{s3_prefix}/marker.txt"
        local_marker_path = get_persistent_artifacts_path(workflow_args, "marker.txt")

        local_marker_path.parent.mkdir(parents=True, exist_ok=True)
        local_marker_path.write_text(next_marker_value, encoding="utf-8")

        logger.info(f"Uploading marker to S3: {marker_s3_key}")
        try:
            await ObjectStore.upload_file(
                source=str(local_marker_path),
                destination=marker_s3_key,
                store_name=UPSTREAM_OBJECT_STORE_NAME,
                retain_local_copy=True,
            )
            logger.info(f"Marker uploaded: {marker_s3_key} → {next_marker_value}")
        except Exception as e:
            logger.error(f"Failed to upload marker: {e}")
            raise

        return {
            "marker_written": True,
            "marker_timestamp": next_marker_value,
            "s3_key": marker_s3_key,
        }

    # =========================================================================
    # State Management Activities
    # =========================================================================

    @activity.defn
    @auto_heartbeater
    async def read_current_state(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """Download current-state folder from S3.

        S3 Path: persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/

        Args:
            workflow_args: Workflow arguments

        Returns:
            Updated workflow_args with current_state_available and paths
        """
        try:
            args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))

            s3_prefix = get_persistent_s3_prefix(workflow_args)
            current_state_s3_prefix = f"{s3_prefix}/current-state"
            current_state_dir = get_persistent_artifacts_path(workflow_args, "current-state")

            current_state_dir.mkdir(parents=True, exist_ok=True)

            logger.info(f"Downloading current-state from S3: {current_state_s3_prefix}")

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
                    logger.info(f"Current-state downloaded with {json_count} JSON files")
                else:
                    logger.info("Current-state downloaded but empty")
            except Exception as e:
                logger.info(f"Current-state not found in S3 (first run): {e}")

            args.current_state_path = str(current_state_dir)
            args.current_state_available = exists
            args.current_state_s3_prefix = current_state_s3_prefix
            args.current_state_json_count = json_count

            workflow_args["metadata"] = args.model_dump(by_alias=True, exclude_none=True)
            return workflow_args
        except Exception as e:
            logger.error(f"Failed to read current-state: {e}")
            raise

    @activity.defn
    @auto_heartbeater
    async def write_current_state(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """Create current-state snapshot with ancestral merge and upload to S3.

        This implements ancestral state preservation:
        1. Tables: Always from current transformed (authoritative)
        2. Columns: Merge current (CREATED/UPDATED) with ancestral (NO CHANGE)
        3. Other entities: Always from current transformed

        S3 Path: persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/

        Args:
            workflow_args: Workflow arguments

        Returns:
            Updated workflow_args with current_state metadata
        """
        workflow_id = workflow_args.get("workflow_id", "unknown")
        run_id = workflow_args.get("workflow_run_id", "unknown")
        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))

        logger.info(f"Starting write_current_state (workflow: {workflow_id}, run: {run_id})")

        if not args.incremental_extraction:
            logger.info("Incremental extraction disabled - skipping write_current_state")
            return workflow_args

        try:
            output_path_str = str(workflow_args.get("output_path", "")).strip()
            if not output_path_str:
                raise FileNotFoundError("No output_path provided")

            # Download transformed files from S3
            transformed_local_path = os.path.join(output_path_str, "transformed")
            transformed_s3_prefix = get_object_store_prefix(transformed_local_path)

            logger.info(f"Downloading transformed files from S3: {transformed_s3_prefix}")
            await ObjectStore.download_prefix(
                source=transformed_s3_prefix,
                store_name=UPSTREAM_OBJECT_STORE_NAME,
            )

            transformed_dir = Path(transformed_local_path)

            # Prepare paths
            s3_prefix = get_persistent_s3_prefix(workflow_args)
            current_state_s3_prefix = f"{s3_prefix}/current-state"
            current_state_dir = get_persistent_artifacts_path(workflow_args, "current-state")

            # Download previous state to temp location
            previous_state_dir = None
            if args.current_state_available and args.current_state_path:
                previous_state_temp_dir = current_state_dir.parent.joinpath(
                    f"{current_state_dir.name}.previous"
                )

                if previous_state_temp_dir.exists():
                    shutil.rmtree(previous_state_temp_dir)
                previous_state_temp_dir.mkdir(parents=True, exist_ok=True)

                logger.info(f"Downloading previous state from S3: {current_state_s3_prefix}")
                try:
                    await download_s3_prefix_with_structure(
                        s3_prefix=current_state_s3_prefix,
                        local_destination=previous_state_temp_dir,
                    )
                    previous_state_dir = previous_state_temp_dir
                except Exception as e:
                    logger.error(f"Failed to download previous state: {e}")
                    if previous_state_temp_dir.exists():
                        shutil.rmtree(previous_state_temp_dir)
                    raise

            table_scope = None

            with DuckDBConnectionManager() as conn_manager:
                conn = conn_manager.connection

                try:
                    # Get table scope
                    table_scope = get_current_table_scope(transformed_dir, conn=conn)
                    if not table_scope or get_scope_length(table_scope) == 0:
                        raise FileNotFoundError(
                            f"No tables found in transformed output: {transformed_dir}"
                        )

                    # Clear and recreate current-state directory
                    if current_state_dir.exists():
                        shutil.rmtree(current_state_dir)
                    current_state_dir.mkdir(parents=True, exist_ok=True)

                    # Copy non-column entities
                    for entity_type in [EntityType.TABLE, EntityType.SCHEMA, EntityType.DATABASE]:
                        entity_dir = transformed_dir.joinpath(entity_type.value)
                        if entity_dir.exists():
                            dest_dir = current_state_dir.joinpath(entity_type.value)
                            count = copy_directory_parallel(
                                entity_dir, dest_dir, max_workers=args.copy_workers
                            )
                            logger.info(f"Copied {count} {entity_type.value} files")

                    # Merge columns
                    merge_result, tables_with_columns = merge_ancestral_columns(
                        current_transformed_dir=transformed_dir,
                        previous_state_dir=previous_state_dir,
                        new_state_dir=current_state_dir,
                        table_scope=table_scope,
                        column_chunk_size=args.column_chunk_size,
                        conn=conn,
                    )

                    table_scope.tables_with_extracted_columns = tables_with_columns
                    total_files = count_json_files_recursive(current_state_dir)

                    logger.info(
                        f"Current-state complete: tables={get_scope_length(table_scope)}, "
                        f"columns={merge_result.columns_total}, files={total_files}"
                    )

                    # Create incremental-diff
                    diff_result = None
                    incremental_diff_dir = None
                    incremental_diff_s3_prefix = None

                    if previous_state_dir and previous_state_dir.exists():
                        incremental_diff_subpath = INCREMENTAL_DIFF_SUBPATH_TEMPLATE.format(
                            run_id=run_id
                        )
                        incremental_diff_dir = get_persistent_artifacts_path(
                            workflow_args, incremental_diff_subpath
                        )
                        incremental_diff_s3_prefix = f"{s3_prefix}/{incremental_diff_subpath}"

                        if incremental_diff_dir.exists():
                            shutil.rmtree(incremental_diff_dir)

                        diff_result = create_incremental_diff(
                            transformed_dir=transformed_dir,
                            incremental_diff_dir=incremental_diff_dir,
                            table_scope=table_scope,
                            previous_state_dir=previous_state_dir,
                            conn=conn,
                            copy_workers=args.copy_workers,
                        )

                        await ObjectStore.upload_prefix(
                            source=str(incremental_diff_dir),
                            destination=incremental_diff_s3_prefix,
                            store_name=UPSTREAM_OBJECT_STORE_NAME,
                        )
                        logger.info(f"Incremental-diff uploaded: {incremental_diff_s3_prefix}")
                    else:
                        logger.info("Skipping incremental-diff (first run)")

                finally:
                    if table_scope:
                        close_scope(table_scope)

            # Cleanup temp previous state
            if previous_state_dir and previous_state_dir.exists():
                try:
                    shutil.rmtree(previous_state_dir)
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp directory: {e}")

            # Upload current-state
            await ObjectStore.upload_prefix(
                source=str(current_state_dir),
                destination=current_state_s3_prefix,
                store_name=UPSTREAM_OBJECT_STORE_NAME,
            )
            logger.info(f"Current-state uploaded: {current_state_s3_prefix}")

            args.current_state_path = str(current_state_dir)
            args.current_state_files = total_files
            args.current_state_s3_prefix = current_state_s3_prefix
            if diff_result:
                args.incremental_diff_path = str(incremental_diff_dir) if incremental_diff_dir else None
                args.incremental_diff_s3_prefix = incremental_diff_s3_prefix
                args.incremental_diff_files = diff_result.total_files
            else:
                args.incremental_diff_path = None
                args.incremental_diff_s3_prefix = None
                args.incremental_diff_files = 0

            workflow_args["metadata"] = args.model_dump(by_alias=True, exclude_none=True)
            return workflow_args

        except Exception as e:
            logger.error(f"Failed to write current-state: {e}")
            raise

    # =========================================================================
    # Column Extraction Activities
    # =========================================================================

    @activity.defn
    @auto_heartbeater
    async def prepare_column_extraction_queries(
        self, workflow_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Prepare batched column extraction queries for incremental/backfill tables.

        Args:
            workflow_args: Workflow arguments

        Returns:
            Dict with query_files list and metadata
        """
        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))

        if not args.incremental_extraction:
            logger.info("Incremental disabled - skipping prepare_column_extraction_queries")
            return {"query_files": [], "skipped": True, "reason": "incremental_disabled"}

        if not args.is_incremental_ready():
            logger.info("Not incremental-ready - skipping batched column extraction")
            return {"query_files": [], "skipped": True, "reason": "not_incremental_ready"}

        if not self.incremental_column_sql:
            raise ValueError("incremental_column_sql not set")

        # Download transformed files
        output_path_str = str(workflow_args.get("output_path", "")).strip()
        transformed_local_path = os.path.join(output_path_str, "transformed")
        transformed_s3_prefix = get_object_store_prefix(transformed_local_path)

        await ObjectStore.download_prefix(
            source=transformed_s3_prefix,
            store_name=UPSTREAM_OBJECT_STORE_NAME,
        )

        transformed_dir = get_transformed_dir(workflow_args)

        # Find backfill tables
        previous_state_dir = None
        if args.current_state_available and args.current_state_path:
            previous_state_dir = Path(args.current_state_path)

        backfill_tables = get_backfill_tables(transformed_dir, previous_state_dir)

        # Get tables needing extraction
        df, changed_count, backfill_count, _ = get_tables_needing_column_extraction(
            transformed_dir, backfill_tables
        )

        total_tables = changed_count + backfill_count
        if total_tables == 0:
            logger.info("No tables need column extraction")
            return {"query_files": [], "total_tables": 0}

        # Resolve SQL template
        resolved_sql = self.resolve_database_placeholders(
            self.incremental_column_sql, workflow_args
        )

        # Generate batched queries
        queries_dir = ensure_runtime_queries_dir(workflow_args, "column_batches")

        query_files = list(
            write_batched_queries_from_df(
                template_sql=resolved_sql,
                df=df,
                batch_size=args.column_batch_size,
                output_dir=queries_dir,
                schema_name=args.system_schema_name,
                marker=args.marker_timestamp or "",
                render_fn=self.render_column_sql,
            )
        )

        logger.info(
            f"Prepared {len(query_files)} query batches for {total_tables} tables "
            f"(changed={changed_count}, backfill={backfill_count})"
        )

        return {
            "query_files": [str(f) for f in query_files],
            "total_tables": total_tables,
            "changed_tables": changed_count,
            "backfill_tables": backfill_count,
            "batch_size": args.column_batch_size,
        }

    @activity.defn
    @auto_heartbeater
    async def execute_single_column_batch(
        self, workflow_args: Dict[str, Any], query_file: str, batch_index: int
    ) -> ActivityStatistics | None:
        """Execute a single column extraction batch.

        Args:
            workflow_args: Workflow arguments
            query_file: Path to SQL query file
            batch_index: Index of this batch

        Returns:
            ActivityStatistics with execution results
        """
        from application_sdk.activities.metadata_extraction.sql import (
            BaseSQLMetadataExtractionActivities,
            BaseSQLMetadataExtractionActivitiesState,
        )

        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))

        query_path = Path(query_file)
        if not query_path.exists():
            raise FileNotFoundError(f"Query file not found: {query_file}")

        sql_query = query_path.read_text(encoding="utf-8")
        logger.info(f"Executing column batch {batch_index}: {query_file}")

        # Get SQL client from state (assuming self is also a BaseSQLMetadataExtractionActivities)
        if isinstance(self, BaseSQLMetadataExtractionActivities):
            state = cast(
                BaseSQLMetadataExtractionActivitiesState,
                await self._get_state(workflow_args),
            )
            if not state.sql_client:
                raise ValueError("SQL client not initialized")

            base_output_path = workflow_args.get("output_path", "")
            return await self.query_executor(
                sql_client=state.sql_client,
                sql_query=sql_query,
                workflow_args=workflow_args,
                output_path=os.path.join(base_output_path, "raw"),
                typename=f"column_batch_{batch_index}",
            )
        else:
            raise TypeError(
                "execute_single_column_batch requires composition with "
                "BaseSQLMetadataExtractionActivities"
            )

    # =========================================================================
    # Helper Methods (not activities, but useful for workflows)
    # =========================================================================

    def is_incremental_ready(self, workflow_args: Dict[str, Any]) -> bool:
        """Check if all prerequisites for incremental extraction are met.

        Args:
            workflow_args: Workflow arguments

        Returns:
            True if incremental extraction should run
        """
        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))
        return args.is_incremental_ready()

    def is_incremental_enabled(self, workflow_args: Dict[str, Any]) -> bool:
        """Check if incremental extraction is enabled in config.

        Args:
            workflow_args: Workflow arguments

        Returns:
            True if incremental extraction is enabled
        """
        args = WorkflowMetadata.model_validate(workflow_args.get("metadata", {}))
        return args.incremental_extraction


# Export for backward compatibility and discoverability
__all__ = ["IncrementalMetadataExtractionMixin"]
