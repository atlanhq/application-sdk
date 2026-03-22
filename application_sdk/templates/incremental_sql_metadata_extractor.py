"""Incremental SQL metadata extraction App — v3 implementation.

Provides ``IncrementalSqlMetadataExtractor``, a concrete orchestrator for
incremental metadata extraction built on top of ``SqlMetadataExtractor``.

The class provides the full incremental orchestration in ``run()`` and defines
the typed contracts for all tasks. Connector implementers subclass this and
implement the abstract tasks (``fetch_databases``, ``fetch_schemas``,
``fetch_tables``, ``transform_data``, ``build_incremental_column_sql``).

Migration from v2::

    # v2: IncrementalSQLMetadataExtractionActivities
    from application_sdk.activities.metadata_extraction.incremental import (
        IncrementalSQLMetadataExtractionActivities,
    )

    # v3: IncrementalSqlMetadataExtractor
    from application_sdk.templates import IncrementalSqlMetadataExtractor

Example subclass::

    from application_sdk.templates import IncrementalSqlMetadataExtractor
    from application_sdk.app import task
    from application_sdk.templates.contracts.sql_metadata import (
        FetchDatabasesInput, FetchDatabasesOutput,
        FetchSchemasInput, FetchSchemasOutput,
        TransformInput, TransformOutput,
    )
    from application_sdk.templates.contracts.incremental_sql import (
        FetchTablesIncrementalInput, FetchTablesOutput,
    )

    class MyConnectorExtractor(IncrementalSqlMetadataExtractor):
        incremental_table_sql = "SELECT ... WHERE last_modified > '{marker_timestamp}'"
        incremental_column_sql = "SELECT ... WHERE table_id IN ({table_ids})"

        @task(timeout_seconds=1800)
        async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
            ...

        @task(timeout_seconds=1800)
        async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
            ...

        @task(timeout_seconds=1800)
        async def fetch_tables(self, input: FetchTablesIncrementalInput) -> FetchTablesOutput:
            is_incremental = bool(input.marker_timestamp) and input.current_state_available
            sql = self.incremental_table_sql if is_incremental else self.fetch_table_sql
            ...

        @task(timeout_seconds=1800)
        async def transform_data(self, input: TransformInput) -> TransformOutput:
            ...

        def build_incremental_column_sql(self, table_ids, ctx):
            return f"SELECT * FROM columns WHERE table_id IN ({', '.join(table_ids)})"
"""

from __future__ import annotations

import asyncio
import os
from abc import abstractmethod
from typing import ClassVar, List, Optional

from application_sdk.app.task import task
from application_sdk.common.exc_utils import rewrap
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates.contracts.incremental_sql import (
    ExecuteColumnBatchInput,
    ExecuteColumnBatchOutput,
    FetchColumnsIncrementalInput,
    FetchIncrementalMarkerInput,
    FetchIncrementalMarkerOutput,
    FetchTablesIncrementalInput,
    IncrementalExtractionInput,
    IncrementalExtractionOutput,
    IncrementalRunContext,
    PrepareColumnQueriesInput,
    PrepareColumnQueriesOutput,
    ReadCurrentStateInput,
    ReadCurrentStateOutput,
    UpdateMarkerInput,
    UpdateMarkerOutput,
    WriteCurrentStateInput,
    WriteCurrentStateOutput,
)
from application_sdk.templates.contracts.sql_metadata import (
    FetchColumnsOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesOutput,
    TransformInput,
    TransformOutput,
)
from application_sdk.templates.sql_metadata_extractor import SqlMetadataExtractor

logger = get_logger(__name__)

# Maximum number of column batch tasks to fan-out in parallel.
# Matches v2 behaviour exactly (default Temporal fan-out limit).
MAX_CONCURRENT_COLUMN_BATCHES: int = 10


class IncrementalSqlMetadataExtractor(SqlMetadataExtractor):
    """Base class for incremental SQL metadata extraction apps.

    Subclass this and override the abstract ``@task`` methods to implement
    connector-specific extraction logic.  The ``run()`` method is fully
    concrete and orchestrates the end-to-end incremental flow:

    1. **Phase 1 – Prerequisites**: fetch marker + current-state (sequential).
    2. **Phase 2 – Base extraction**: databases, schemas (parallel), then tables.
    3. **Phase 3 – Incremental columns**: batch preparation + parallel execution
       (only when ``ctx.is_incremental_ready()`` is True).
    4. **Phase 4 – Write state**: create and upload the current-state snapshot.
    5. **Phase 5 – Update marker**: persist the new marker after successful state write.

    Concrete infrastructure tasks (``fetch_incremental_marker``,
    ``read_current_state``, ``prepare_column_extraction_queries``,
    ``execute_single_column_batch``, ``write_current_state``,
    ``update_incremental_marker``) are implemented in this class and call the
    helpers from ``application_sdk.common.incremental``.

    Abstract tasks (``fetch_databases``, ``fetch_schemas``, ``fetch_tables``,
    ``transform_data``) must be implemented by subclasses.  ``fetch_columns``
    has a partial default implementation that skips SQL when incremental mode
    is active.

    Class variables:
        incremental_table_sql: SQL template for incremental table extraction;
            ``None`` means fall back to full SQL.
        incremental_column_sql: SQL template for incremental column extraction;
            ``None`` means fall back to full SQL.
    """

    incremental_table_sql: ClassVar[Optional[str]] = None
    incremental_column_sql: ClassVar[Optional[str]] = None

    # ------------------------------------------------------------------
    # Abstract tasks — must be implemented by connector subclasses
    # ------------------------------------------------------------------

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        """Fetch all databases from the source system.

        Override this method in your connector subclass to execute the
        database-listing SQL and return the results.

        The ``input`` carries all standard extraction fields (credentials,
        filters, paths). Use ``input.connection``, ``input.credential_ref``,
        ``input.exclude_filter``, and ``input.include_filter`` as appropriate.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_databases()."
        )

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        """Fetch all schemas from the source system.

        Override this method in your connector subclass to execute the
        schema-listing SQL and return the results.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_schemas()."
        )

    @task(timeout_seconds=1800)
    async def fetch_tables(  # type: ignore[override]
        self, input: FetchTablesIncrementalInput
    ) -> FetchTablesOutput:
        """Fetch tables, switching between full and incremental SQL based on run context.

        This method is abstract — connectors must implement it. The inputs carry
        everything needed to choose the correct SQL:

        **Full extraction** (``input.marker_timestamp`` is empty or
        ``input.current_state_available`` is False):
            Use the standard full-extraction SQL (``self.fetch_table_sql``).

        **Incremental extraction** (both ``input.marker_timestamp`` and
        ``input.current_state_available`` are truthy):
            Use ``self.incremental_table_sql`` with the marker timestamp injected.
            This SQL should filter to only tables modified since the marker.

        Recommended implementation pattern::

            @task(timeout_seconds=1800)
            async def fetch_tables(self, input: FetchTablesIncrementalInput) -> FetchTablesOutput:
                is_incremental = (
                    bool(input.marker_timestamp) and input.current_state_available
                )
                if is_incremental and self.incremental_table_sql:
                    sql = self.incremental_table_sql.replace(
                        "{marker_timestamp}", input.marker_timestamp
                    )
                    sql = self.resolve_database_placeholders(sql, input)
                else:
                    sql = prepare_query(self.fetch_table_sql, input)
                results = await self._sql_client.execute(sql, ...)
                return FetchTablesOutput(
                    tables=[r["table_qn"] for r in results],
                    total_record_count=len(results),
                )

        Note: Do NOT mutate ``self.incremental_table_sql`` or ``self.fetch_table_sql``
        — Temporal workers reuse activity instances across runs, so class-attribute
        mutation would bleed between concurrent executions. Always resolve SQL into
        a local variable.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_tables(). "
            "See the docstring for the incremental SQL switching pattern."
        )

    @task(timeout_seconds=1800)
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        """Transform raw extracted data into the target format.

        This method is abstract — connectors must implement it.

        **Standard extraction** (``input.file_names`` is empty):
            Raw data is read from ``{input.output_path}/raw/{input.typename}/``.

        **Incremental column batch extraction** (``input.file_names`` is non-empty):
            Raw column data from batch execution lives in
            ``{input.output_path}/raw/`` (no typename subdirectory). Use
            ``input.file_names`` to read only those specific parquet files.

        Recommended implementation pattern::

            @task(timeout_seconds=1800)
            async def transform_data(self, input: TransformInput) -> TransformOutput:
                if input.file_names:
                    raw_path = os.path.join(input.output_path, "raw")
                else:
                    raw_path = os.path.join(input.output_path, "raw", input.typename)
                # ... read from raw_path and write to transformed/ ...
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement transform_data()."
        )

    # ------------------------------------------------------------------
    # Abstract helper — must be implemented by connector subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    def build_incremental_column_sql(
        self,
        table_ids: List[str],
        ctx: IncrementalRunContext,
    ) -> str:
        """Build the SQL query string for extracting columns for a batch of tables.

        This is the connector-specific method that constructs the column SQL for
        a batch of table IDs. The SDK handles all orchestration — the connector
        only needs to produce the SQL string.

        Examples by database:
        - Oracle: CTE with ``FROM dual UNION ALL`` syntax
        - ClickHouse: ``WHERE table_id IN (...)``
        - PostgreSQL: ``WHERE table_id = ANY(ARRAY[...])``

        Args:
            table_ids: List of table identifiers (catalog.schema.table format).
            ctx: Current :class:`IncrementalRunContext` (for marker, connection info,
                chunk size, etc.).

        Returns:
            Fully rendered SQL query string ready for execution.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement build_incremental_column_sql()."
        )

    # ------------------------------------------------------------------
    # Optional hook — override for database-specific SQL placeholder resolution
    # ------------------------------------------------------------------

    def resolve_database_placeholders(
        self,
        sql: str,
        input: FetchTablesIncrementalInput,  # noqa: A002
    ) -> str:
        """Replace database-specific placeholders in SQL templates.

        Override this method to handle placeholders like ``{system_schema}``
        (e.g., ``SYS`` for Oracle).

        The default implementation is a no-op — only override when needed.

        Args:
            sql: SQL template with placeholders.
            input: The task input (carries connection, credential, filter info).

        Returns:
            SQL string with database-specific placeholders replaced.
        """
        return sql

    # ------------------------------------------------------------------
    # Concrete tasks — fetch_columns with incremental skip logic
    # ------------------------------------------------------------------

    @task(timeout_seconds=1800)
    async def fetch_columns(  # type: ignore[override]
        self, input: FetchColumnsIncrementalInput
    ) -> FetchColumnsOutput:
        """Fetch columns for a full (non-incremental) extraction run.

        This method is called from run() in two scenarios:

        1. **Full extraction** (first run or incremental_extraction=False): both
           ``input.marker_timestamp`` and ``input.current_state_available`` will be
           falsy. Implement this method to execute the full column SQL and write
           chunk files to ``input.output_path``.

        2. **Incremental run** (``input.marker_timestamp`` and
           ``input.current_state_available`` are both truthy): this method is still
           called but returns immediately with zero counts — column extraction is
           handled by ``execute_single_column_batch()`` instead.

        Implementers: check ``input.marker_timestamp`` and
        ``input.current_state_available`` at the top of your override::

            @task(timeout_seconds=1800)
            async def fetch_columns(self, input: FetchColumnsIncrementalInput) -> FetchColumnsOutput:
                if input.marker_timestamp and input.current_state_available:
                    # Incremental mode — columns handled by batch extraction
                    return FetchColumnsOutput()
                # Full extraction path
                results = await self._sql_client.execute(self.fetch_column_sql, ...)
                return FetchColumnsOutput(total_record_count=results.count)
        """
        if input.marker_timestamp and input.current_state_available:
            logger.info(
                "Skipping generic column fetch — incremental mode active, "
                "columns will be extracted per-batch by execute_single_column_batch()"
            )
            return FetchColumnsOutput()
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_columns() for full extraction. "
            "See the docstring for the expected implementation pattern."
        )

    # ------------------------------------------------------------------
    # Concrete infrastructure tasks — call common/incremental/ helpers
    # ------------------------------------------------------------------

    @task(timeout_seconds=300)
    async def fetch_incremental_marker(
        self, input: FetchIncrementalMarkerInput
    ) -> FetchIncrementalMarkerOutput:
        """Download and process the incremental marker from persistent S3 storage.

        Retrieves the marker written by the previous successful run and generates
        the ``next_marker`` timestamp that will be written after this run succeeds.

        On the first run (no previous marker) returns an empty ``marker_timestamp``
        and a freshly generated ``next_marker_timestamp``.
        """
        from application_sdk.common.incremental.marker import fetch_marker_from_storage

        marker, next_marker = await fetch_marker_from_storage(
            connection_qualified_name=input.connection_qualified_name,
            application_name=input.application_name,
            existing_marker=input.existing_marker,
            prepone_enabled=input.prepone_enabled,
            prepone_hours=input.prepone_hours,
        )
        return FetchIncrementalMarkerOutput(
            marker_timestamp=marker or "",
            next_marker_timestamp=next_marker,
        )

    @task(timeout_seconds=300)
    async def read_current_state(
        self, input: ReadCurrentStateInput
    ) -> ReadCurrentStateOutput:
        """Download the current-state snapshot from persistent S3 storage.

        Returns metadata about the downloaded state including whether it
        exists (``current_state_available``). On the first run the state does
        not exist and the task returns with ``current_state_available=False``.
        """
        from application_sdk.common.incremental.state.state_reader import (
            download_current_state,
        )

        state_dir, s3_prefix, exists, json_count = await download_current_state(
            connection_qualified_name=input.connection_qualified_name,
            application_name=input.application_name,
        )
        return ReadCurrentStateOutput(
            current_state_path=str(state_dir),
            current_state_s3_prefix=s3_prefix,
            current_state_available=exists,
            current_state_json_count=json_count,
        )

    @task(timeout_seconds=3600)
    async def prepare_column_extraction_queries(
        self, input: PrepareColumnQueriesInput
    ) -> PrepareColumnQueriesOutput:
        """Identify tables needing column extraction and write batched table-ID JSON files.

        Downloads transformed data from S3, identifies changed/backfill tables,
        batches their IDs into JSON files, and uploads the batch files to S3 so
        that parallel ``execute_single_column_batch`` tasks can download only
        their specific batch file.

        Returns counts of batches, changed tables, and backfill tables.
        Zero batches means no incremental column extraction is needed.
        """
        import json

        from application_sdk.common.incremental.column_extraction import (
            get_backfill_tables,
            get_tables_needing_column_extraction,
        )
        from application_sdk.common.incremental.helpers import (
            download_s3_prefix_with_structure,
            get_persistent_artifacts_path,
        )
        from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
        from application_sdk.execution._temporal.activity_utils import (
            get_object_store_prefix,
        )
        from application_sdk.services.objectstore import ObjectStore

        if not input.output_path:
            raise FileNotFoundError(
                "No output_path provided in PrepareColumnQueriesInput"
            )

        # Step 1: Download transformed files from S3
        transformed_local_path = os.path.join(input.output_path, "transformed")
        transformed_s3_prefix = get_object_store_prefix(transformed_local_path)
        import pathlib

        transformed_dir = pathlib.Path(transformed_local_path)
        transformed_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Downloading transformed files from S3", prefix=transformed_s3_prefix
        )
        await ObjectStore.download_prefix(
            source=transformed_s3_prefix,
            destination=str(transformed_dir),
            store_name=UPSTREAM_OBJECT_STORE_NAME,
        )

        batch_size = input.column_batch_size
        logger.info("Preparing column extraction batches", batch_size=batch_size)

        # Step 2: Download previous current-state for backfill comparison
        previous_current_state_dir = None
        if input.current_state_available and input.current_state_s3_prefix:
            previous_current_state_dir = get_persistent_artifacts_path(
                input.connection_qualified_name,
                "current-state",
                input.application_name,
            )
            previous_current_state_dir.mkdir(parents=True, exist_ok=True)

            table_dir = previous_current_state_dir.joinpath("table")
            has_table_files = table_dir.exists() and any(table_dir.glob("*.json"))

            if not has_table_files:
                logger.info(
                    "Downloading current-state from S3 for backfill comparison",
                    prefix=input.current_state_s3_prefix,
                )
                try:
                    await download_s3_prefix_with_structure(
                        s3_prefix=input.current_state_s3_prefix,
                        local_destination=previous_current_state_dir,
                    )
                    logger.info(
                        "Current-state downloaded",
                        path=str(previous_current_state_dir),
                    )
                except Exception:
                    logger.warning(
                        "Failed to download current-state for backfill",
                        exc_info=True,
                    )
                    previous_current_state_dir = None
            else:
                table_file_count = len(list(table_dir.glob("*.json")))
                logger.info(
                    "Previous current-state already present",
                    table_file_count=table_file_count,
                )

        # Step 3: Find backfill tables using DuckDB
        logger.info("Analyzing table state to identify backfill candidates...")
        backfill_qns = get_backfill_tables(transformed_dir, previous_current_state_dir)
        backfill_count_for_log = len(backfill_qns) if backfill_qns else 0
        logger.info("Found tables needing backfill", count=backfill_count_for_log)

        # Step 4: Get tables needing column extraction using Daft
        filtered_df, changed_count, backfill_count, _no_change_count = (
            get_tables_needing_column_extraction(transformed_dir, backfill_qns)
        )

        total_tables = changed_count + backfill_count
        if total_tables == 0:
            logger.info("No tables need column extraction")
            return PrepareColumnQueriesOutput(
                total_batches=0,
                changed_tables=0,
                backfill_tables=0,
                total_tables=0,
            )

        logger.info(
            "Batching tables into groups",
            total_tables=total_tables,
            batch_size=batch_size,
        )

        # Step 5: Batch table_ids into JSON files
        batches_dir = pathlib.Path(input.output_path).joinpath(
            "batches", "column-table-ids"
        )
        batches_dir.mkdir(parents=True, exist_ok=True)

        batch_idx = 0
        total_tables_batched = 0
        current_batch: list[str] = []

        for row in filtered_df.select("table_id").iter_rows():
            current_batch.append(row["table_id"])

            if len(current_batch) >= batch_size:
                batch_file = batches_dir / f"batch-{batch_idx}.json"
                batch_file.write_text(json.dumps(current_batch), encoding="utf-8")
                batch_idx += 1
                total_tables_batched += len(current_batch)
                current_batch = []

        if current_batch:
            batch_file = batches_dir / f"batch-{batch_idx}.json"
            batch_file.write_text(json.dumps(current_batch), encoding="utf-8")
            batch_idx += 1
            total_tables_batched += len(current_batch)

        logger.info(
            "Created batch files",
            batch_count=batch_idx,
            total_tables=total_tables_batched,
            directory=str(batches_dir),
        )

        # Step 6: Upload batch files to S3
        batches_s3_prefix = get_object_store_prefix(str(batches_dir))
        logger.info("Uploading batch files to S3", prefix=batches_s3_prefix)
        await ObjectStore.upload_prefix(
            source=str(batches_dir),
            destination=batches_s3_prefix,
            store_name=UPSTREAM_OBJECT_STORE_NAME,
            retain_local_copy=True,
        )

        return PrepareColumnQueriesOutput(
            total_batches=batch_idx,
            changed_tables=changed_count,
            backfill_tables=backfill_count,
            total_tables=total_tables,
            batches_s3_prefix=batches_s3_prefix,
            batches_local_dir=str(batches_dir),
        )

    @task(timeout_seconds=3600)
    async def execute_single_column_batch(
        self, input: ExecuteColumnBatchInput
    ) -> ExecuteColumnBatchOutput:
        """Execute a single incremental column extraction batch.

        Downloads the batch-specific JSON file of table IDs from S3, calls
        ``build_incremental_column_sql()`` to construct the SQL, then executes
        the query and writes results to the output path.

        The SDK handles all orchestration; the connector only needs to implement
        ``build_incremental_column_sql()``.
        """
        import json
        import pathlib

        from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
        from application_sdk.execution._temporal.activity_utils import (
            get_object_store_prefix,
        )
        from application_sdk.services.objectstore import ObjectStore

        if not input.output_path:
            raise ValueError("output_path is required in ExecuteColumnBatchInput")

        # Reconstruct IncrementalRunContext from input fields for SQL building
        ctx = IncrementalRunContext(
            workflow_id=input.workflow_id,
            output_prefix=input.output_prefix,
            output_path=input.output_path,
            marker_timestamp=input.marker_timestamp or None,
            current_state_available=input.current_state_available,
            column_chunk_size=input.column_chunk_size,
            application_name=input.application_name,
        )

        batches_dir = pathlib.Path(input.output_path).joinpath(
            "batches", "column-table-ids"
        )
        batches_dir.mkdir(parents=True, exist_ok=True)

        batch_filename = f"batch-{input.batch_index}.json"
        batch_file = batches_dir / batch_filename

        # Download only the specific batch file
        if input.batches_s3_prefix:
            batches_s3_prefix = input.batches_s3_prefix
        else:
            batches_s3_prefix = get_object_store_prefix(str(batches_dir))

        await ObjectStore.download_file(
            source=f"{batches_s3_prefix}/{batch_filename}",
            destination=str(batch_file),
            store_name=UPSTREAM_OBJECT_STORE_NAME,
        )

        if not batch_file.exists():
            logger.warning("Batch file not found", path=str(batch_file))
            return ExecuteColumnBatchOutput(
                batch_index=input.batch_index,
                records=0,
                status="not_found",
            )

        table_ids = json.loads(batch_file.read_text(encoding="utf-8"))

        logger.info(
            "Executing column batch",
            batch=input.batch_index + 1,
            total_batches=input.total_batches,
            table_count=len(table_ids),
        )

        sql = self.build_incremental_column_sql(table_ids, ctx)
        batch_records = await self.execute_column_sql(sql, input, ctx)

        logger.info(
            "Column batch complete",
            batch=input.batch_index + 1,
            total_batches=input.total_batches,
            records=batch_records,
        )

        return ExecuteColumnBatchOutput(
            batch_index=input.batch_index,
            records=batch_records,
            status="success",
        )

    async def execute_column_sql(
        self,
        sql: str,
        input: ExecuteColumnBatchInput,  # noqa: A002
        ctx: IncrementalRunContext,
    ) -> int:
        """Execute a column SQL query and return the record count.

        This is a helper for ``execute_single_column_batch``. Override in
        subclasses to hook into the query execution layer.

        The default implementation raises ``NotImplementedError`` — subclasses
        that use a SQL client should override this.

        Args:
            sql: Fully rendered SQL query to execute.
            input: The batch task input (paths, credentials, etc.).
            ctx: IncrementalRunContext for this run.

        Returns:
            Number of column records extracted.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement execute_column_sql() "
            "to execute the SQL built by build_incremental_column_sql()."
        )

    @task(timeout_seconds=3600)
    async def write_current_state(
        self, input: WriteCurrentStateInput
    ) -> WriteCurrentStateOutput:
        """Create the current-state snapshot, merge ancestral columns, and upload to S3.

        Steps:
        1. Build ``transformed_dir`` from ``input.output_path``.
        2. Download previous state via ``prepare_previous_state()``.
        3. Get ``current_state_dir`` via ``get_persistent_artifacts_path()``.
        4. Call ``create_current_state_snapshot()`` (merge + diff + upload).
        5. Clean up temporary previous state directory.
        """
        from application_sdk.common.incremental.helpers import (
            get_persistent_artifacts_path,
            get_persistent_s3_prefix,
        )
        from application_sdk.common.incremental.state.state_writer import (
            cleanup_previous_state,
            create_current_state_snapshot,
            download_transformed_data,
            prepare_previous_state,
        )

        run_id = input.workflow_run_id or input.workflow_id or "unknown"
        conn_qn = input.connection.attributes.qualified_name
        app_name = input.application_name

        logger.info(
            "Starting write_current_state with ancestral merge",
            workflow_id=input.workflow_id,
            run_id=run_id,
        )

        previous_state_dir = None
        try:
            transformed_dir = await download_transformed_data(input.output_path)

            s3_prefix = get_persistent_s3_prefix(conn_qn, app_name)
            current_state_dir = get_persistent_artifacts_path(
                conn_qn, "current-state", app_name
            )

            previous_state_dir = await prepare_previous_state(
                connection_qualified_name=conn_qn,
                current_state_available=input.current_state_available,
                current_state_dir=current_state_dir,
                application_name=app_name,
            )

            result = await create_current_state_snapshot(
                connection_qualified_name=conn_qn,
                transformed_dir=transformed_dir,
                previous_state_dir=previous_state_dir,
                current_state_dir=current_state_dir,
                s3_prefix=s3_prefix,
                run_id=run_id,
                application_name=app_name,
                copy_workers=input.copy_workers,
                column_chunk_size=input.column_chunk_size,
            )

            return WriteCurrentStateOutput(
                current_state_path=str(result.current_state_dir),
                current_state_s3_prefix=result.current_state_s3_prefix,
                current_state_files=result.total_files,
                incremental_diff_path=(
                    str(result.incremental_diff_dir)
                    if result.incremental_diff_dir
                    else ""
                ),
                incremental_diff_s3_prefix=result.incremental_diff_s3_prefix or "",
                incremental_diff_files=result.incremental_diff_files,
            )

        except Exception as e:
            raise rewrap(e, "Failed to write current-state") from e
        finally:
            cleanup_previous_state(previous_state_dir)

    @task(timeout_seconds=120)
    async def update_incremental_marker(
        self, input: UpdateMarkerInput
    ) -> UpdateMarkerOutput:
        """Persist the next marker timestamp to persistent S3 storage.

        Called at the very end of a successful run to record the new
        high-watermark for the next incremental run.

        If ``input.next_marker_timestamp`` is empty, returns immediately
        without writing anything.
        """
        if not input.next_marker_timestamp:
            return UpdateMarkerOutput(marker_written=False)

        from application_sdk.common.incremental.marker import persist_marker_to_storage

        result = await persist_marker_to_storage(
            connection_qualified_name=input.connection_qualified_name,
            marker_value=input.next_marker_timestamp,
            application_name=input.application_name,
        )
        return UpdateMarkerOutput(
            marker_written=result.get("marker_written", False),
            marker_timestamp=result.get("marker_timestamp", ""),
            s3_key=result.get("s3_key", ""),
        )

    # ------------------------------------------------------------------
    # Orchestration — run()
    # ------------------------------------------------------------------

    async def run(  # type: ignore[override]
        self, input: IncrementalExtractionInput
    ) -> IncrementalExtractionOutput:
        """Orchestrate the full incremental metadata extraction pipeline.

        See the class docstring for a description of the five phases.
        Override individual tasks to customise connector behaviour; override
        this method only if you need to change the orchestration structure.
        """
        from temporalio import workflow

        workflow_id = input.workflow_id
        run_id = workflow.info().run_id
        conn_qn = input.connection.attributes.qualified_name
        application_name = os.getenv("ATLAN_APPLICATION_NAME", "")

        cred_ref = input.credential_ref
        if cred_ref is None and input.credential_guid:
            from application_sdk.credentials import legacy_credential_ref

            cred_ref = legacy_credential_ref(input.credential_guid)

        ctx = IncrementalRunContext(
            workflow_id=workflow_id,
            workflow_run_id=run_id,
            output_prefix=input.output_prefix,
            output_path=input.output_path,
            connection_qualified_name=conn_qn,
            connection_name=input.connection.attributes.name,
            application_name=application_name,
            incremental_extraction=input.incremental_extraction,
            column_batch_size=input.column_batch_size,
            column_chunk_size=input.column_chunk_size,
            copy_workers=input.copy_workers,
            prepone_marker_timestamp=input.prepone_marker_timestamp,
            prepone_marker_hours=input.prepone_marker_hours,
        )

        # ================================================================
        # PHASE 1: Incremental prerequisites (sequential)
        # ================================================================
        marker_result = await self.fetch_incremental_marker(
            FetchIncrementalMarkerInput(
                connection_qualified_name=ctx.connection_qualified_name,
                application_name=ctx.application_name,
                prepone_enabled=ctx.prepone_marker_timestamp,
                prepone_hours=float(ctx.prepone_marker_hours),
            )
        )
        ctx.marker_timestamp = marker_result.marker_timestamp or None
        ctx.next_marker_timestamp = marker_result.next_marker_timestamp

        state_result = await self.read_current_state(
            ReadCurrentStateInput(
                connection_qualified_name=ctx.connection_qualified_name,
                application_name=ctx.application_name,
            )
        )
        ctx.current_state_available = state_result.current_state_available
        ctx.current_state_path = state_result.current_state_path
        ctx.current_state_s3_prefix = state_result.current_state_s3_prefix
        ctx.current_state_json_count = state_result.current_state_json_count

        # ================================================================
        # PHASE 2: Base metadata extraction
        # ================================================================
        # Databases and schemas can run in parallel
        db_result, schema_result = await asyncio.gather(
            self.fetch_databases(
                FetchDatabasesInput(
                    workflow_id=workflow_id,
                    connection=input.connection,
                    credential_guid=input.credential_guid,
                    credential_ref=cred_ref,
                    output_prefix=input.output_prefix,
                    output_path=input.output_path,
                    exclude_filter=input.exclude_filter,
                    include_filter=input.include_filter,
                    temp_table_regex=input.temp_table_regex,
                    source_tag_prefix=input.source_tag_prefix,
                )
            ),
            self.fetch_schemas(
                FetchSchemasInput(
                    workflow_id=workflow_id,
                    connection=input.connection,
                    credential_guid=input.credential_guid,
                    credential_ref=cred_ref,
                    output_prefix=input.output_prefix,
                    output_path=input.output_path,
                    exclude_filter=input.exclude_filter,
                    include_filter=input.include_filter,
                    temp_table_regex=input.temp_table_regex,
                    source_tag_prefix=input.source_tag_prefix,
                )
            ),
        )

        # Tables run after databases/schemas
        table_result = await self.fetch_tables(
            FetchTablesIncrementalInput(
                workflow_id=workflow_id,
                connection=input.connection,
                credential_guid=input.credential_guid,
                credential_ref=cred_ref,
                output_prefix=input.output_prefix,
                output_path=input.output_path,
                exclude_filter=input.exclude_filter,
                include_filter=input.include_filter,
                temp_table_regex=input.temp_table_regex,
                source_tag_prefix=input.source_tag_prefix,
                incremental_extraction=ctx.incremental_extraction,
                marker_timestamp=ctx.marker_timestamp or "",
                current_state_available=ctx.current_state_available,
                column_chunk_size=ctx.column_chunk_size,
            )
        )

        # Columns: skip SQL if incremental_ready (handled by batch extraction)
        column_result = await self.fetch_columns(
            FetchColumnsIncrementalInput(
                workflow_id=workflow_id,
                connection=input.connection,
                credential_guid=input.credential_guid,
                credential_ref=cred_ref,
                output_prefix=input.output_prefix,
                output_path=input.output_path,
                exclude_filter=input.exclude_filter,
                include_filter=input.include_filter,
                temp_table_regex=input.temp_table_regex,
                source_tag_prefix=input.source_tag_prefix,
                incremental_extraction=ctx.incremental_extraction,
                marker_timestamp=ctx.marker_timestamp or "",
                current_state_available=ctx.current_state_available,
                column_chunk_size=ctx.column_chunk_size,
            )
        )

        # ================================================================
        # PHASE 3: Incremental column batch extraction (if incremental_ready)
        # ================================================================
        total_columns = column_result.total_record_count
        column_batches_executed = 0

        if ctx.is_incremental_ready():
            prep_result = await self.prepare_column_extraction_queries(
                PrepareColumnQueriesInput(
                    workflow_id=workflow_id,
                    connection=input.connection,
                    credential_guid=input.credential_guid,
                    credential_ref=cred_ref,
                    output_prefix=input.output_prefix,
                    output_path=input.output_path,
                    exclude_filter=input.exclude_filter,
                    include_filter=input.include_filter,
                    temp_table_regex=input.temp_table_regex,
                    source_tag_prefix=input.source_tag_prefix,
                    incremental_extraction=ctx.incremental_extraction,
                    marker_timestamp=ctx.marker_timestamp or "",
                    current_state_available=ctx.current_state_available,
                    column_chunk_size=ctx.column_chunk_size,
                    connection_qualified_name=ctx.connection_qualified_name,
                    current_state_s3_prefix=ctx.current_state_s3_prefix,
                    column_batch_size=ctx.column_batch_size,
                    application_name=ctx.application_name,
                )
            )
            ctx.total_column_batches = prep_result.total_batches
            ctx.changed_tables = prep_result.changed_tables
            ctx.backfill_tables = prep_result.backfill_tables

            if prep_result.total_batches > 0:
                all_batch_results = []
                # Execute in chunks of MAX_CONCURRENT_COLUMN_BATCHES
                for chunk_start in range(
                    0, prep_result.total_batches, MAX_CONCURRENT_COLUMN_BATCHES
                ):
                    chunk_end = min(
                        chunk_start + MAX_CONCURRENT_COLUMN_BATCHES,
                        prep_result.total_batches,
                    )
                    chunk_results = await asyncio.gather(
                        *[
                            self.execute_single_column_batch(
                                ExecuteColumnBatchInput(
                                    workflow_id=workflow_id,
                                    connection=input.connection,
                                    credential_guid=input.credential_guid,
                                    credential_ref=cred_ref,
                                    output_prefix=input.output_prefix,
                                    output_path=input.output_path,
                                    exclude_filter=input.exclude_filter,
                                    include_filter=input.include_filter,
                                    temp_table_regex=input.temp_table_regex,
                                    source_tag_prefix=input.source_tag_prefix,
                                    incremental_extraction=ctx.incremental_extraction,
                                    marker_timestamp=ctx.marker_timestamp or "",
                                    current_state_available=ctx.current_state_available,
                                    column_chunk_size=ctx.column_chunk_size,
                                    batch_index=i,
                                    total_batches=prep_result.total_batches,
                                    batches_s3_prefix=prep_result.batches_s3_prefix,
                                    application_name=ctx.application_name,
                                )
                            )
                            for i in range(chunk_start, chunk_end)
                        ]
                    )
                    all_batch_results.extend(chunk_results)
                total_columns = sum(r.records for r in all_batch_results)
                column_batches_executed = len(all_batch_results)

        # ================================================================
        # PHASE 4: Write current state
        # ================================================================
        ws_result = await self.write_current_state(
            WriteCurrentStateInput(
                workflow_id=workflow_id,
                connection=input.connection,
                credential_guid=input.credential_guid,
                credential_ref=cred_ref,
                output_prefix=input.output_prefix,
                output_path=input.output_path,
                exclude_filter=input.exclude_filter,
                include_filter=input.include_filter,
                temp_table_regex=input.temp_table_regex,
                source_tag_prefix=input.source_tag_prefix,
                incremental_extraction=ctx.incremental_extraction,
                marker_timestamp=ctx.marker_timestamp or "",
                current_state_available=ctx.current_state_available,
                column_chunk_size=ctx.column_chunk_size,
                workflow_run_id=run_id,
                current_state_s3_prefix=ctx.current_state_s3_prefix,
                copy_workers=ctx.copy_workers,
                application_name=ctx.application_name,
            )
        )
        ctx.current_state_files = ws_result.current_state_files
        ctx.incremental_diff_path = ws_result.incremental_diff_path
        ctx.incremental_diff_s3_prefix = ws_result.incremental_diff_s3_prefix
        ctx.incremental_diff_files = ws_result.incremental_diff_files

        # ================================================================
        # PHASE 5: Update marker (only after successful state write)
        # ================================================================
        marker_updated = False
        if input.incremental_extraction and ctx.next_marker_timestamp:
            um_result = await self.update_incremental_marker(
                UpdateMarkerInput(
                    connection_qualified_name=ctx.connection_qualified_name,
                    next_marker_timestamp=ctx.next_marker_timestamp,
                    application_name=ctx.application_name,
                )
            )
            marker_updated = um_result.marker_written

        return IncrementalExtractionOutput(
            workflow_id=workflow_id,
            success=True,
            databases_extracted=db_result.total_record_count,
            schemas_extracted=schema_result.total_record_count,
            tables_extracted=table_result.total_record_count,
            columns_extracted=total_columns,
            current_state_files=ctx.current_state_files,
            incremental_diff_files=ctx.incremental_diff_files,
            column_batches_executed=column_batches_executed,
            changed_tables=ctx.changed_tables,
            backfill_tables=ctx.backfill_tables,
            marker_updated=marker_updated,
        )
