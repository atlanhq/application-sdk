"""SqlApp — consolidated SQL metadata extraction template.

Replaces ``SqlMetadataExtractor``, ``SqlQueryExtractor``, and
``BaseMetadataExtractor`` with a single App class that provides:

* Standard ``@task`` methods for SQL metadata extraction
  (databases, schemas, tables, columns, views, procedures)
* Per-entity transform tasks using **pyatlan_v9 asset mapper**
  (replaces YAML transformer)
* ``upload_to_atlan`` for output migration
* ``build_task_input()`` as public API for ``run()`` overrides (BLDX-1138)
* ``fetch_views()`` and ``fetch_procedures()`` stubs (BLDX-1139)
* Parallel per-entity transforms via ``asyncio.gather()`` (BLDX-1140)

Usage::

    from application_sdk.templates.sql_app import SqlApp
    from application_sdk.clients.sql import BaseSQLClient
    from application_sdk.clients.models import DatabaseConfig

    class MySQLClient(BaseSQLClient):
        DB_CONFIG = DatabaseConfig(
            template="mysql+pymysql://{username}:{password}@{host}:{port}/{database}",
            required=["username", "password", "host", "port"],
        )

    class MySQLApp(SqlApp):
        sql_client_class = MySQLClient

        fetch_database_sql = "SELECT SCHEMA_NAME as database_name FROM ..."
        fetch_schema_sql = "SELECT SCHEMA_NAME as schema_name FROM ..."
        fetch_table_sql = "SELECT TABLE_NAME as table_name FROM ..."
        fetch_column_sql = "SELECT COLUMN_NAME as column_name FROM ..."

        def map_table(self, record, connection_qn):
            from pyatlan_v9.model.assets import Table
            return Table(
                qualified_name=f"{connection_qn}/{record['database_name']}/{record['schema_name']}/{record['table_name']}",
                name=record["table_name"],
            )

        def map_column(self, record, connection_qn):
            from pyatlan_v9.model.assets import Column
            return Column(...)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.common.sql_filters import normalize_filters
from application_sdk.constants import TEMPORARY_PATH
from application_sdk.credentials import CredentialResolver, legacy_credential_ref
from application_sdk.execution import build_output_path
from application_sdk.infrastructure.context import get_infrastructure
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.transfer import upload as transfer_upload
from application_sdk.templates.contracts.base_metadata_extraction import (
    UploadInput,
    UploadOutput,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    ExtractionTaskInput,
    FetchColumnsInput,
    FetchColumnsOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchProceduresInput,
    FetchProceduresOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesInput,
    FetchTablesOutput,
    FetchViewsInput,
    FetchViewsOutput,
    TransformInput,
    TransformOutput,
)

if TYPE_CHECKING:
    from application_sdk.clients.sql import BaseSQLClient
    from application_sdk.credentials.ref import CredentialRef

logger = get_logger(__name__)

_ET = TypeVar("_ET", bound=ExtractionTaskInput)


class SqlApp(App):
    """Consolidated SQL metadata extraction App.

    Subclass and set:
    - ``sql_client_class``: Your ``BaseSQLClient`` subclass
    - ``fetch_database_sql``, ``fetch_schema_sql``, etc.: SQL templates
    - ``map_table()``, ``map_column()``, etc.: Asset mapper functions

    The base ``run()`` orchestrates: fetch → transform (parallel) → upload.
    """

    _app_registered: ClassVar[bool] = True  # abstract template, not concrete

    # ── SQL client ──────────────────────────────────────────────────────
    sql_client_class: ClassVar[type[BaseSQLClient] | None] = None

    # ── SQL templates (set in subclass) ─────────────────────────────────
    fetch_database_sql: ClassVar[str] = ""
    fetch_schema_sql: ClassVar[str] = ""
    fetch_table_sql: ClassVar[str] = ""
    fetch_column_sql: ClassVar[str] = ""
    fetch_view_sql: ClassVar[str] = ""
    fetch_procedure_sql: ClassVar[str] = ""

    # ── Temp table regex SQL fragments ──────────────────────────────────
    extract_temp_table_regex_table_sql: ClassVar[str] = ""
    extract_temp_table_regex_column_sql: ClassVar[str] = ""

    # ── Column name mappings (override if connector aliases differently) ─
    database_name_column: ClassVar[str] = "database_name"
    schema_name_column: ClassVar[str] = "schema_name"
    table_name_column: ClassVar[str] = "table_name"

    # =====================================================================
    # Public API: build_task_input (BLDX-1138)
    # =====================================================================

    @staticmethod
    def build_task_input(
        input_cls: type[_ET],
        src: ExtractionInput,
        *,
        cred_ref: CredentialRef | None = None,
    ) -> _ET:
        """Build a typed task input from the top-level extraction input.

        Public API — use this when overriding ``run()`` to construct typed
        inputs for individual ``@task`` methods.

        Args:
            input_cls: The task input class (e.g. ``FetchDatabasesInput``).
            src: The top-level ``ExtractionInput`` from the workflow.
            cred_ref: Optional credential reference for secret resolution.

        Returns:
            An instance of *input_cls* populated from *src*.
        """
        return input_cls(
            workflow_id=src.workflow_id,
            connection=src.connection,
            credential_guid=src.credential_guid,
            credential_ref=cred_ref,
            output_prefix=src.output_prefix,
            output_path=src.output_path,
            exclude_filter=src.exclude_filter,
            include_filter=src.include_filter,
            temp_table_regex=src.temp_table_regex,
            source_tag_prefix=getattr(src, "source_tag_prefix", ""),
        )

    # =====================================================================
    # @task: Metadata extraction
    # =====================================================================

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        """Fetch database/catalog names via SQL."""
        if not self.fetch_database_sql:
            logger.warning("fetch_database_sql not set — returning empty")
            return FetchDatabasesOutput(chunk_count=0, total_record_count=0)

        client = await self._init_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_database_sql.strip(), input)
            result = await client.get_results(sql)
            count = len(result) if result is not None else 0
            logger.info("Fetched %d databases", count)
            # Write to output
            output_path = self._resolve_output_path(input)
            if count > 0 and output_path:
                output_dir = Path(output_path) / "raw" / "database"
                output_dir.mkdir(parents=True, exist_ok=True)
                result.to_parquet(str(output_dir / "chunk-0-part0.parquet"))
            return FetchDatabasesOutput(
                chunk_count=1 if count > 0 else 0, total_record_count=count
            )
        finally:
            await client.close()

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        """Fetch schema names via SQL."""
        if not self.fetch_schema_sql:
            logger.warning("fetch_schema_sql not set — returning empty")
            return FetchSchemasOutput(chunk_count=0, total_record_count=0)

        client = await self._init_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_schema_sql.strip(), input)
            result = await client.get_results(sql)
            count = len(result) if result is not None else 0
            logger.info("Fetched %d schemas", count)
            output_path = self._resolve_output_path(input)
            if count > 0 and output_path:
                output_dir = Path(output_path) / "raw" / "schema"
                output_dir.mkdir(parents=True, exist_ok=True)
                result.to_parquet(str(output_dir / "chunk-0-part0.parquet"))
            return FetchSchemasOutput(
                chunk_count=1 if count > 0 else 0, total_record_count=count
            )
        finally:
            await client.close()

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
        """Fetch table metadata via SQL."""
        if not self.fetch_table_sql:
            logger.warning("fetch_table_sql not set — returning empty")
            return FetchTablesOutput(chunk_count=0, total_record_count=0)

        client = await self._init_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_table_sql.strip(), input)
            result = await client.get_results(sql)
            count = len(result) if result is not None else 0
            logger.info("Fetched %d tables", count)
            output_path = self._resolve_output_path(input)
            if count > 0 and output_path:
                output_dir = Path(output_path) / "raw" / "table"
                output_dir.mkdir(parents=True, exist_ok=True)
                result.to_parquet(str(output_dir / "chunk-0-part0.parquet"))
            return FetchTablesOutput(
                chunk_count=1 if count > 0 else 0, total_record_count=count
            )
        finally:
            await client.close()

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        """Fetch column metadata via SQL."""
        if not self.fetch_column_sql:
            logger.warning("fetch_column_sql not set — returning empty")
            return FetchColumnsOutput(chunk_count=0, total_record_count=0)

        client = await self._init_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_column_sql.strip(), input)
            result = await client.get_results(sql)
            count = len(result) if result is not None else 0
            logger.info("Fetched %d columns", count)
            output_path = self._resolve_output_path(input)
            if count > 0 and output_path:
                output_dir = Path(output_path) / "raw" / "column"
                output_dir.mkdir(parents=True, exist_ok=True)
                result.to_parquet(str(output_dir / "chunk-0-part0.parquet"))
            return FetchColumnsOutput(
                chunk_count=1 if count > 0 else 0, total_record_count=count
            )
        finally:
            await client.close()

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def fetch_views(self, input: FetchViewsInput) -> FetchViewsOutput:
        """Fetch view metadata via SQL. Override in subclass if needed (BLDX-1139)."""
        if not self.fetch_view_sql:
            return FetchViewsOutput(chunk_count=0, total_record_count=0)

        client = await self._init_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_view_sql.strip(), input)
            result = await client.get_results(sql)
            count = len(result) if result is not None else 0
            logger.info("Fetched %d views", count)
            output_path = self._resolve_output_path(input)
            if count > 0 and output_path:
                output_dir = Path(output_path) / "raw" / "view"
                output_dir.mkdir(parents=True, exist_ok=True)
                result.to_parquet(str(output_dir / "chunk-0-part0.parquet"))
            return FetchViewsOutput(
                chunk_count=1 if count > 0 else 0, total_record_count=count
            )
        finally:
            await client.close()

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def fetch_procedures(
        self, input: FetchProceduresInput
    ) -> FetchProceduresOutput:
        """Fetch stored procedure metadata. Override in subclass if needed."""
        if not self.fetch_procedure_sql:
            return FetchProceduresOutput(chunk_count=0, total_record_count=0)

        client = await self._init_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_procedure_sql.strip(), input)
            result = await client.get_results(sql)
            count = len(result) if result is not None else 0
            logger.info("Fetched %d procedures", count)
            output_path = self._resolve_output_path(input)
            if count > 0 and output_path:
                output_dir = Path(output_path) / "raw" / "procedure"
                output_dir.mkdir(parents=True, exist_ok=True)
                result.to_parquet(str(output_dir / "chunk-0-part0.parquet"))
            return FetchProceduresOutput(
                chunk_count=1 if count > 0 else 0, total_record_count=count
            )
        finally:
            await client.close()

    # =====================================================================
    # @task: Per-entity transforms (BLDX-1140) — asset mapper pattern
    # =====================================================================

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_databases(self, input: TransformInput) -> TransformOutput:
        """Transform raw database records to Atlan assets using map_database()."""
        return await self._transform_entity("database", self.map_database, input)

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_schemas(self, input: TransformInput) -> TransformOutput:
        """Transform raw schema records to Atlan assets using map_schema()."""
        return await self._transform_entity("schema", self.map_schema, input)

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_tables(self, input: TransformInput) -> TransformOutput:
        """Transform raw table records to Atlan assets using map_table()."""
        return await self._transform_entity("table", self.map_table, input)

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_columns(self, input: TransformInput) -> TransformOutput:
        """Transform raw column records to Atlan assets using map_column()."""
        return await self._transform_entity("column", self.map_column, input)

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_procedures(self, input: TransformInput) -> TransformOutput:
        """Transform raw procedure records to Atlan Procedure assets using map_procedure().

        Writes to the ``extras-procedure`` entity type so the publish-app can
        parse the SQL ``definition`` field and derive lineage (Process / ColumnProcess
        entities) automatically — matching the legacy Argo crawler output.
        """
        return await self._transform_entity(
            "extras-procedure", self.map_procedure, input
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        """Legacy single transform — override for custom logic, or use
        per-entity transforms (transform_tables, transform_columns, etc.)."""
        raise NotImplementedError(
            "Override transform_data() or use per-entity transforms "
            "(transform_tables, transform_columns, etc.)"
        )

    # ── Upload ──────────────────────────────────────────────────────────

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def upload_to_atlan(self, input: UploadInput) -> UploadOutput:
        """Upload transformed output to the upstream Atlan store."""
        output_path = input.output_path or os.path.join(
            TEMPORARY_PATH, build_output_path()
        )
        if not output_path:
            return UploadOutput(migrated_files=0, total_files=0)

        result = await transfer_upload(
            local_path=output_path,
            storage_path=build_output_path(),
        )
        file_count = result.ref.file_count if result.ref else 0
        logger.info("Uploaded %d files to Atlan (synced=%s)", file_count, result.synced)
        return UploadOutput(
            migrated_files=file_count,
            total_files=file_count,
        )

    # =====================================================================
    # Asset mapper stubs — connectors override these
    # =====================================================================

    def map_database(self, record: dict[str, Any], connection_qn: str) -> Any:
        """Map a raw database record to a pyatlan_v9 Asset. Override in subclass."""
        raise NotImplementedError("Override map_database() in your SqlApp subclass")

    def map_schema(self, record: dict[str, Any], connection_qn: str) -> Any:
        """Map a raw schema record to a pyatlan_v9 Asset. Override in subclass."""
        raise NotImplementedError("Override map_schema() in your SqlApp subclass")

    def map_table(self, record: dict[str, Any], connection_qn: str) -> Any:
        """Map a raw table record to a pyatlan_v9 Asset. Override in subclass."""
        raise NotImplementedError("Override map_table() in your SqlApp subclass")

    def map_column(self, record: dict[str, Any], connection_qn: str) -> Any:
        """Map a raw column record to a pyatlan_v9 Asset. Override in subclass."""
        raise NotImplementedError("Override map_column() in your SqlApp subclass")

    # =====================================================================
    # run() — default orchestration
    # =====================================================================

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        """Default extraction orchestration.

        1. Resolve credentials
        2. Fetch metadata in parallel (databases, schemas, tables, columns)
        3. Transform per-entity in parallel using asset mappers
        4. Upload to Atlan

        Override for custom orchestration (e.g. sequential fetches, multi-DB).
        Use ``build_task_input()`` to construct typed inputs.
        """
        cred_ref = self._resolve_credential_ref(input)

        # ── Fetch metadata in parallel ──────────────────────────────────
        db_input = self.build_task_input(FetchDatabasesInput, input, cred_ref=cred_ref)
        schema_input = self.build_task_input(
            FetchSchemasInput, input, cred_ref=cred_ref
        )
        table_input = self.build_task_input(FetchTablesInput, input, cred_ref=cred_ref)
        column_input = self.build_task_input(
            FetchColumnsInput, input, cred_ref=cred_ref
        )

        db_result, schema_result, table_result, column_result = await asyncio.gather(
            self.fetch_databases(db_input),
            self.fetch_schemas(schema_input),
            self.fetch_tables(table_input),
            self.fetch_columns(column_input),
        )

        logger.info(
            "Metadata fetch complete: databases=%d, schemas=%d, tables=%d, columns=%d",
            db_result.total_record_count,
            schema_result.total_record_count,
            table_result.total_record_count,
            column_result.total_record_count,
        )

        # ── Transform per-entity in parallel ────────────────────────────
        transform_input = self.build_task_input(
            TransformInput, input, cred_ref=cred_ref
        )

        try:
            await asyncio.gather(
                self.transform_databases(transform_input),
                self.transform_schemas(transform_input),
                self.transform_tables(transform_input),
                self.transform_columns(transform_input),
            )
        except NotImplementedError:
            # Fall back to single transform_data() for backward compat
            logger.info("Per-entity transforms not implemented, using transform_data()")
            await self.transform_data(transform_input)

        # ── Upload ──────────────────────────────────────────────────────
        # Always call upload — upload_to_atlan auto-resolves output_path in activity context.
        upload_input = UploadInput(
            output_path=input.output_path,
            output_prefix=input.output_prefix,
        )
        await self.upload_to_atlan(upload_input)

        connection_qn = ""
        if input.connection and input.connection.attributes:
            connection_qn = input.connection.attributes.qualified_name or ""

        return ExtractionOutput(
            databases_extracted=db_result.total_record_count,
            schemas_extracted=schema_result.total_record_count,
            tables_extracted=table_result.total_record_count,
            columns_extracted=column_result.total_record_count,
            connection_qualified_name=connection_qn,
        )

    # =====================================================================
    # Internal helpers
    # =====================================================================

    @staticmethod
    def _resolve_output_path(input: ExtractionTaskInput) -> str:
        """Resolve output_path — auto-set from build_output_path() in activity context."""
        if not input.output_path:
            resolved = build_output_path()
            logger.info("Auto-resolved output_path: %s", resolved)
            return os.path.join(TEMPORARY_PATH, resolved)
        return input.output_path

    async def _init_sql_client(self, input: ExtractionTaskInput) -> BaseSQLClient:
        """Initialize and return a SQL client from credentials."""
        if self.sql_client_class is None:
            raise ValueError("sql_client_class must be set on the SqlApp subclass")

        client = self.sql_client_class()

        # Resolve credentials
        creds: dict[str, Any] = {}
        if input.credential_ref and input.credential_ref.credential_guid:
            infra = get_infrastructure()
            secret_store = infra.secret_store if infra else None
            if secret_store:
                resolver = CredentialResolver(secret_store)
                creds = await resolver.resolve_raw(input.credential_ref) or {}
        elif input.credential_guid:
            infra = get_infrastructure()
            secret_store = infra.secret_store if infra else None
            if secret_store:
                resolver = CredentialResolver(secret_store)
                creds = (
                    await resolver.resolve_raw(
                        legacy_credential_ref(input.credential_guid)
                    )
                    or {}
                )

        await client.load(credentials=creds)
        return client

    def _prepare_sql(self, sql: str, input: ExtractionTaskInput) -> str:
        """Substitute filter placeholders in SQL template."""
        exclude_filter = input.exclude_filter or ""
        include_filter = input.include_filter or ""

        # Handle dict filters (from AE) — normalize to regex
        if isinstance(exclude_filter, dict):
            normalized = normalize_filters(exclude_filter, False)
            exclude_regex = "|".join(normalized) if normalized else "^$"
        else:
            exclude_regex = exclude_filter or "^$"

        if isinstance(include_filter, dict):
            normalized = normalize_filters(include_filter, True)
            include_regex = "|".join(normalized) if normalized else ".*"
        else:
            include_regex = include_filter or ".*"

        # Temp table regex
        temp_table_sql = ""
        if hasattr(input, "temp_table_regex") and input.temp_table_regex:
            if self.extract_temp_table_regex_table_sql:
                temp_table_sql = self.extract_temp_table_regex_table_sql.replace(
                    "{exclude_table_regex}", input.temp_table_regex
                )

        sql = sql.replace("{normalized_exclude_regex}", exclude_regex)
        sql = sql.replace("{normalized_include_regex}", include_regex)
        sql = sql.replace("{temp_table_regex_sql}", temp_table_sql)

        return sql

    def _resolve_credential_ref(self, input: ExtractionInput) -> CredentialRef | None:
        """Resolve credential ref from extraction input."""

        if hasattr(input, "credential_ref") and input.credential_ref:
            return input.credential_ref
        if input.credential_guid:
            return legacy_credential_ref(input.credential_guid)
        return None

    @staticmethod
    def _sanitize_value(v: Any) -> Any:
        """Sanitize a single value for JSON serialization."""
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return None
        # pandas NaT / NaN types
        if hasattr(v, "__class__") and v.__class__.__name__ in ("NaTType", "NAType"):
            return None
        return v

    @classmethod
    def _sanitize_nan(cls, obj: Any) -> Any:
        """Recursively replace NaN, Inf, NaT with None for valid JSON."""
        if isinstance(obj, dict):
            return {k: cls._sanitize_nan(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [cls._sanitize_nan(v) for v in obj]
        return cls._sanitize_value(obj)

    async def _transform_entity(
        self,
        entity_type: str,
        mapper_fn: Any,
        input: TransformInput,
    ) -> TransformOutput:
        """Generic per-entity transform: read raw parquet → mapper → JSONL."""
        import pandas as pd  # noqa: PLC0415

        output_path = self._resolve_output_path(input)
        raw_dir = Path(output_path) / "raw" / entity_type if output_path else None
        if raw_dir is None or not raw_dir.exists():
            return TransformOutput(total_record_count=0)

        # Read all parquet files in the raw directory
        parquet_files = list(raw_dir.glob("*.parquet"))
        if not parquet_files:
            return TransformOutput(total_record_count=0)

        connection_qn = ""
        connection_name = ""
        if hasattr(input, "connection") and input.connection:
            attrs = getattr(input.connection, "attributes", None)
            if attrs:
                connection_qn = getattr(attrs, "qualified_name", "") or ""
                connection_name = getattr(attrs, "name", "") or ""

        output_dir = Path(output_path) / "transformed" / entity_type
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "entities.json"

        count = 0
        with open(output_file, "wb") as f:
            for pf in parquet_files:
                df = pd.read_parquet(str(pf))
                for _, row in df.iterrows():
                    record = row.to_dict()
                    asset = mapper_fn(record, connection_qn)
                    # Inject connectionName if the mapper returned a dict
                    if isinstance(asset, dict) and connection_name:
                        asset.setdefault("attributes", {}).setdefault(
                            "connectionName", connection_name
                        )
                    # Write as JSONL — sanitize NaN/Inf before serialization.
                    # pandas converts SQL NULLs to NaN which is invalid JSON.
                    if isinstance(asset, dict):
                        asset = self._sanitize_nan(asset)
                    if hasattr(asset, "to_nested_dict"):
                        entity_bytes = json.dumps(asset.to_nested_dict()).encode()
                    elif hasattr(asset, "model_dump"):
                        entity_bytes = json.dumps(asset.model_dump()).encode()
                    elif isinstance(asset, dict):
                        entity_bytes = json.dumps(asset).encode()
                    else:
                        entity_bytes = json.dumps(record).encode()
                    f.write(entity_bytes + b"\n")
                    count += 1

        logger.info("Transformed %s: %d records", entity_type, count)
        return TransformOutput(total_record_count=count)
