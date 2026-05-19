"""SqlApp — consolidated SQL metadata extraction template.

Replaces ``SqlMetadataExtractor``, ``SqlQueryExtractor``, and
``BaseMetadataExtractor`` with a single App class that provides:

* ``extract_*`` ``@task`` methods that **stream SQL rows verbatim into
  raw JSONL** under ``raw/<entity>/records.json`` — one pass, no parquet
  intermediate, no pandas/pyarrow.
* ``transform_*`` ``@task`` methods that read the raw JSONL and run each
  record through the connector's ``map_*`` function, writing the result
  to ``transformed/<entity>/entities.json``. The activity boundary keeps
  transform retry-able without re-running the SQL extraction.
* Per-entity asset mappers (``map_database`` / ``map_schema`` /
  ``map_table`` / ``map_column`` / ``map_procedure``) — direct
  pyatlan_v9 ``Asset`` construction, no YAML transformer.
* ``upload_to_atlan`` for output migration to the upstream object store.
* ``build_task_input()`` as public API for ``run()`` overrides (BLDX-1138).
* ``extract_views()`` / ``extract_procedures()`` /
  ``transform_views()`` / ``transform_procedures()`` for the optional
  flows (BLDX-1139).
* Parallel extract + transform via ``asyncio.gather()`` (BLDX-1140).

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
import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, Union

import orjson
from temporalio import workflow as _temporal_workflow

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.common.sql_filters import normalize_filters
from application_sdk.constants import (
    APPLICATION_NAME,
    TEMPORARY_PATH,
    WORKFLOW_OUTPUT_PATH_TEMPLATE,
)
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.credentials import CredentialResolver, legacy_credential_ref
from application_sdk.credentials.ref import CredentialRef
from application_sdk.execution import build_output_path, get_object_store_prefix
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
    ExtractionTaskOutput,
    TransformInput,
    TransformOutput,
)

if TYPE_CHECKING:
    from pyatlan_v9.model.assets import Asset

    from application_sdk.clients.sql import BaseSQLClient

logger = get_logger(__name__)

_ET = TypeVar("_ET", bound=ExtractionTaskInput)

#: Mapper-function signature: takes a raw record dict + connection
#: qualified name, returns a pyatlan_v9 Asset (preferred) or a plain dict
#: (legacy / connector-specific shapes the v3 publisher accepts).
_MapperFn = Callable[[dict[str, Any], str], Union["Asset", dict[str, Any]]]

#: SQL row batch size used by ``_extract_entity`` when streaming results
#: through ``client.run_query()``. Caps the in-flight set at
#: ``_EXTRACT_BATCH_SIZE * row_size`` regardless of total result size.
_EXTRACT_BATCH_SIZE: int = 10_000


def _orjson_default(obj: Any) -> Any:
    """Fallback serialiser for orjson — covers types it doesn't handle natively.

    orjson natively serialises ``str``, ``int``, ``float``, ``bool``, ``None``,
    ``list``, ``dict``, ``datetime``, ``date``, ``time``, ``UUID`` and
    ``dataclass`` instances. SQL drivers commonly return ``Decimal`` for
    numeric columns and occasionally ``bytes`` for blob columns; both fall
    back to a JSON-safe representation here.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(  # orjson default= protocol requires TypeError to signal non-serializable
        f"Object of type {type(obj).__name__} is not JSON-serializable"
    )


class SqlApp(App):
    """Consolidated SQL metadata extraction App.

    Subclass and set:
    - ``sql_client_class``: Your ``BaseSQLClient`` subclass
    - ``fetch_database_sql``, ``fetch_schema_sql``, etc.: SQL templates
    - ``map_table()``, ``map_column()``, etc.: Asset mapper functions

    The base ``run()`` orchestrates: extract (parallel) → transform
    (parallel) → upload. ``extract_*`` activities stream SQL rows into
    ``raw/<entity>/records.json``; ``transform_*`` activities read that
    raw JSONL and run records through the matching ``map_*`` function,
    writing Atlan assets to ``transformed/<entity>/entities.json``.

    **Parallel-extract assumption:** the default ``run()`` issues all four
    extract tasks (databases, schemas, tables, columns) concurrently via
    ``asyncio.gather()``. Each SQL template must therefore be fully
    self-contained — no cross-entity parameterisation. Connectors that
    need to iterate tables per-schema, paginate by database, or otherwise
    sequence fetches must override ``run()`` and call the ``extract_*``
    / ``transform_*`` activities directly in the order they need.
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
            input_cls: The task input class (e.g. ``ExtractionTaskInput``).
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
    # @task: Metadata extraction — SQL stream → raw JSONL
    # =====================================================================
    #
    # Each ``extract_*`` task streams its SQL result through
    # ``client.run_query`` (server-side cursor, ``_EXTRACT_BATCH_SIZE`` rows
    # per batch) and writes rows verbatim to ``raw/<entity>/records.json``
    # — one JSON object per line, no mapping. Mapping happens in
    # ``transform_*`` so the activity boundary is a durable retry point
    # and so future change-detection can compare raw streams between runs
    # before paying mapper cost.

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_databases(
        self, input: ExtractionTaskInput
    ) -> ExtractionTaskOutput:
        """Stream database/catalog rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="database",
            sql_template=self.fetch_database_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_schemas(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream schema rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="schema",
            sql_template=self.fetch_schema_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_tables(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream table rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="table",
            sql_template=self.fetch_table_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_columns(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream column rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="column",
            sql_template=self.fetch_column_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_views(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream view rows from SQL into raw JSONL."""
        return await self._extract_entity(
            entity_type="view",
            sql_template=self.fetch_view_sql,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def extract_procedures(
        self, input: ExtractionTaskInput
    ) -> ExtractionTaskOutput:
        """Stream stored-procedure rows from SQL into raw JSONL.

        Writes to the ``extras-procedure`` entity type so the publish-app
        can parse the SQL ``definition`` field and derive lineage
        (Process / ColumnProcess entities) — matches the legacy Argo
        crawler output layout.
        """
        return await self._extract_entity(
            entity_type="extras-procedure",
            sql_template=self.fetch_procedure_sql,
            input=input,
        )

    # =====================================================================
    # @task: Per-entity transform — raw JSONL → mapper → transformed JSONL
    # =====================================================================
    #
    # Each ``transform_*`` task reads ``raw/<entity>/records.json`` line by
    # line, runs the dict through the connector's ``map_*`` function, and
    # writes the resulting Atlan asset to ``transformed/<entity>/entities.json``.
    # Activity boundary keeps transform retry-able without re-running the
    # SQL extraction.

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_databases(self, input: TransformInput) -> TransformOutput:
        """Map raw database records to Atlan assets via ``map_database``."""
        return await self._transform_entity(
            entity_type="database",
            mapper_fn=self.map_database,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_schemas(self, input: TransformInput) -> TransformOutput:
        """Map raw schema records to Atlan assets via ``map_schema``."""
        return await self._transform_entity(
            entity_type="schema",
            mapper_fn=self.map_schema,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_tables(self, input: TransformInput) -> TransformOutput:
        """Map raw table records to Atlan assets via ``map_table``."""
        return await self._transform_entity(
            entity_type="table",
            mapper_fn=self.map_table,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_columns(self, input: TransformInput) -> TransformOutput:
        """Map raw column records to Atlan assets via ``map_column``."""
        return await self._transform_entity(
            entity_type="column",
            mapper_fn=self.map_column,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_views(self, input: TransformInput) -> TransformOutput:
        """Map raw view records to Atlan assets via ``map_table``.

        Views go through ``map_table`` because Atlan models View as a
        Table specialisation (typeName ``View``) — connectors differentiate
        via the ``table_kind`` field on the record.
        """
        return await self._transform_entity(
            entity_type="view",
            mapper_fn=self.map_table,
            input=input,
        )

    @task(
        timeout_seconds=1800, heartbeat_timeout_seconds=120, auto_heartbeat_seconds=30
    )
    async def transform_procedures(self, input: TransformInput) -> TransformOutput:
        """Map raw procedure records to Atlan assets via ``map_procedure``."""
        return await self._transform_entity(
            entity_type="extras-procedure",
            mapper_fn=self.map_procedure,
            input=input,
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

    def map_database(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw database record to a pyatlan_v9 Asset. Override in subclass."""
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapDatabaseUnimplementedError,
        )

        raise MapDatabaseUnimplementedError()

    def map_schema(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw schema record to a pyatlan_v9 Asset. Override in subclass."""
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapSchemaUnimplementedError,
        )

        raise MapSchemaUnimplementedError()

    def map_table(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw table record to a pyatlan_v9 Asset. Override in subclass."""
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapTableUnimplementedError,
        )

        raise MapTableUnimplementedError()

    def map_column(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw column record to a pyatlan_v9 Asset. Override in subclass."""
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapColumnUnimplementedError,
        )

        raise MapColumnUnimplementedError()

    def map_procedure(
        self, record: dict[str, Any], connection_qn: str
    ) -> Asset | dict[str, Any]:
        """Map a raw procedure record to a pyatlan_v9 Asset. Override in
        subclass if your connector emits procedures via the
        ``transform_procedures()`` task. Without an override the task
        raises ``MapProcedureUnimplementedError`` rather than ``AttributeError``.
        """
        from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
            MapProcedureUnimplementedError,
        )

        raise MapProcedureUnimplementedError()

    # =====================================================================
    # run() — default orchestration
    # =====================================================================

    async def run(  # type: ignore[override]
        self, input: ExtractionInput
    ) -> ExtractionOutput:
        """Default extraction orchestration.

        1. Resolve credentials
        2. Extract metadata in parallel (databases, schemas, tables,
           columns) — each task streams SQL rows into raw JSONL under
           ``raw/<entity>/records.json``.
        3. Transform per-entity in parallel — each task reads the raw
           JSONL and runs records through the matching ``map_*`` function
           into ``transformed/<entity>/entities.json``.
        4. Upload to Atlan.

        Override for custom orchestration (e.g. sequential fetches, multi-DB).
        Use ``build_task_input()`` to construct typed inputs.
        """
        cred_ref = self._resolve_credential_ref(input)

        task_input = self.build_task_input(
            ExtractionTaskInput, input, cred_ref=cred_ref
        )

        # ── Phase 1: Extract — SQL rows → raw JSONL ─────────────────────
        db_result, schema_result, table_result, column_result = await asyncio.gather(
            self.extract_databases(task_input),
            self.extract_schemas(task_input),
            self.extract_tables(task_input),
            self.extract_columns(task_input),
        )

        logger.info(
            "Extraction complete: databases=%d, schemas=%d, tables=%d, columns=%d",
            db_result.total_record_count,
            schema_result.total_record_count,
            table_result.total_record_count,
            column_result.total_record_count,
        )

        # ── Phase 2: Transform — raw JSONL → mapper → transformed JSONL ──
        # Thread each extract's ``raw_file`` ``FileReference`` into the
        # matching transform's input. The activity interceptor:
        #   1. uploaded each ephemeral raw_file ref after the extract
        #      activity completed (now durable, with an object-store
        #      storage_path);
        #   2. will download the durable ref onto whichever worker pod
        #      runs the matching transform (with SHA-256 sidecar
        #      verification — handles the case where transform lands on
        #      a different pod than extract).
        # This is the BLDX-1281 cross-worker fix: no manual download_file
        # plumbing inside the transform, the framework does it.
        await asyncio.gather(
            self.transform_databases(
                self._build_transform_input(task_input, db_result.raw_file)
            ),
            self.transform_schemas(
                self._build_transform_input(task_input, schema_result.raw_file)
            ),
            self.transform_tables(
                self._build_transform_input(task_input, table_result.raw_file)
            ),
            self.transform_columns(
                self._build_transform_input(task_input, column_result.raw_file)
            ),
        )

        # ── Phase 3: Upload ─────────────────────────────────────────────
        # Always call upload — upload_to_atlan auto-resolves output_path in activity context.
        upload_input = UploadInput(
            output_path=input.output_path,
        )
        await self.upload_to_atlan(upload_input)

        connection_qn = ""
        if input.connection and input.connection.attributes:
            connection_qn = input.connection.attributes.qualified_name or ""

        # Derive the output path using the workflow context (run() is a Temporal
        # workflow method, not an activity — build_output_path() would fail with
        # "Not in activity context"). workflow.info() is always available here.
        # get_object_store_prefix() strips TEMPORARY_PATH to yield the S3 key.
        if input.output_path:
            resolved_base = input.output_path
        else:
            info = _temporal_workflow.info()
            resolved_base = os.path.join(
                TEMPORARY_PATH,
                WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
                    application_name=APPLICATION_NAME or self._app_name or "app",
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                ),
            )

        return ExtractionOutput(
            databases_extracted=db_result.total_record_count,
            schemas_extracted=schema_result.total_record_count,
            tables_extracted=table_result.total_record_count,
            columns_extracted=column_result.total_record_count,
            connection_qualified_name=connection_qn,
            transformed_data_prefix=get_object_store_prefix(
                os.path.join(resolved_base, "transformed")
            ),
            # Expose the resolved local base path so subclasses can derive
            # additional prefixes (e.g. lineage-specific dirs) without calling
            # workflow.info() a second time.
            output_path=resolved_base,
        )

    # =====================================================================
    # Internal helpers
    # =====================================================================

    @staticmethod
    def _build_transform_input(
        base: ExtractionTaskInput,
        raw_file: FileReference | None,
    ) -> TransformInput:
        """Build a ``TransformInput`` from ``base`` carrying ``raw_file``.

        Called by ``run()`` to thread the durable ``FileReference``
        returned by each ``extract_*`` activity (as
        ``ExtractionTaskOutput.raw_file``) into the matching
        ``transform_*`` activity's input. The resulting ``TransformInput``
        inherits every field from ``base`` plus the new ``raw_file``
        ref; the activity interceptor will materialise the ref on
        whichever worker pod ends up running the transform, with
        SHA-256 sidecar verification — see ``storage.file_ref_sync``.
        """
        return TransformInput(
            **base.model_dump(exclude_none=False),
            raw_file=raw_file,
        )

    @staticmethod
    def _resolve_output_path(input: ExtractionTaskInput) -> str:
        """Resolve output_path — auto-set from build_output_path() in activity context."""
        if not input.output_path:
            resolved = build_output_path()
            logger.info("Auto-resolved output_path: %s", resolved)
            return os.path.join(TEMPORARY_PATH, resolved)
        return input.output_path

    async def _init_sql_client(self, input: ExtractionTaskInput) -> BaseSQLClient:
        """Initialize and return a SQL client from credentials.

        ``ExtractionTaskInput`` only carries ``credential_ref`` / ``credential_guid``
        (no ``agent_json`` / ``extraction_method``). The SDR/direct routing
        decision happens upstream in ``_resolve_credential_ref(ExtractionInput)``
        which populates ``credential_ref`` on the task input via
        ``build_task_input``. So here we just consume that ref.
        """
        if self.sql_client_class is None:
            from application_sdk.templates.sql_app_errors import (  # noqa: PLC0415
                SqlClientClassNotSetError,
            )

            raise SqlClientClassNotSetError()

        client = self.sql_client_class()
        creds: dict[str, Any] = {}

        ref: CredentialRef | None = input.credential_ref
        if ref is None and input.credential_guid:
            ref = legacy_credential_ref(input.credential_guid)

        if ref is not None:
            infra = get_infrastructure()
            secret_store = infra.secret_store if infra else None
            if secret_store:
                resolver = CredentialResolver(secret_store)
                creds = await resolver.resolve_raw(ref) or {}

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
        """Resolve credential ref from extraction input.

        Handles both direct (credential_guid) and SDR (agent_json) modes
        via :meth:`CredentialRef.resolve`, falling back to
        ``legacy_credential_ref`` for older inputs.
        """
        if hasattr(input, "credential_ref") and input.credential_ref:
            return input.credential_ref
        # CredentialRef.resolve handles both direct (credential_guid) and
        # SDR (agent_json) modes — works on ExtractionInput which has the
        # full set of routing fields. Per-task ExtractionTaskInput doesn't
        # expose extraction_method/agent_json and isn't routed through here.
        try:
            return CredentialRef.resolve(input)
        except (ValueError, TypeError):
            logger.warning(
                "CredentialRef.resolve failed; falling back to legacy_credential_ref or None",
                exc_info=True,
            )
            if input.credential_guid:
                return legacy_credential_ref(input.credential_guid)
            return None

    async def _extract_entity(
        self,
        *,
        entity_type: str,
        sql_template: str,
        input: ExtractionTaskInput,
    ) -> ExtractionTaskOutput:
        """Stream SQL rows verbatim into ``raw/<entity>/records.json`` JSONL.

        1. Open SQL client, render the template with the input filters.
        2. Stream rows in batches of ``_EXTRACT_BATCH_SIZE`` via the
           server-side cursor that ``client.run_query`` exposes — each
           batch is a ``list[dict[str, Any]]`` keyed by lower-cased column
           names. Memory stays bounded regardless of total result size.
        3. Write each row to JSONL. Mapping is deliberately *not* done
           here — it runs in the matching ``transform_*`` activity so the
           activity boundary stays a durable retry point and so future
           change-detection can compare raw streams between runs.
        """
        if not sql_template:
            logger.warning("No SQL configured for %s — skipping", entity_type)
            return ExtractionTaskOutput(typename=entity_type, total_record_count=0)

        output_path = self._resolve_output_path(input)
        if not output_path:
            return ExtractionTaskOutput(typename=entity_type, total_record_count=0)

        output_dir = Path(output_path) / "raw" / entity_type
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / "records.json"

        client = await self._init_sql_client(input)
        count = 0
        try:
            sql = self._prepare_sql(sql_template.strip(), input)
            with open(output_file, "wb") as f:
                async for batch in client.run_query(
                    sql, batch_size=_EXTRACT_BATCH_SIZE
                ):
                    for record in batch:
                        f.write(orjson.dumps(record, default=_orjson_default))
                        f.write(b"\n")
                        count += 1
        finally:
            await client.close()

        logger.info("Extracted %s: %d raw records", entity_type, count)
        # Emit a ``FileReference`` so the activity interceptor uploads the
        # raw JSONL to the object store after the extract activity completes
        # and marks the ref durable. ``run()`` then threads this durable ref
        # into the matching transform's ``TransformInput`` — the interceptor
        # re-downloads it onto whichever worker pod picks up the transform
        # (with SHA-256 sidecar verification, so a transform that lands on
        # a different pod than the extract still sees a verified-fresh
        # local copy). This is how cross-worker fault tolerance for the
        # raw → transform handoff is provided without any explicit
        # ``download_file`` plumbing in the template (BLDX-1281).
        #
        # Tier=RETAINED matches every other SDK upload path
        # (``UploadInput``, ``App.upload``, ``base_metadata_extractor``,
        # ``sql_metadata_extractor``) — the ref lands under the
        # run-scoped ``<run_prefix>/file_refs/<uuid>.json`` (i.e.
        # ``artifacts/apps/<app>/workflows/<wf>/<run>/file_refs/...``)
        # which is the only prefix Atlan's blob-storage gateway
        # permits writes to in production deployments. The default
        # ``TRANSIENT`` tier writes to bare ``file_refs/...`` and gets
        # rejected with 403 ``code 1009 Invalid Path``.
        raw_ref = (
            FileReference.from_local(output_file, tier=StorageTier.RETAINED)
            if count > 0
            else None
        )
        return ExtractionTaskOutput(
            typename=entity_type,
            total_record_count=count,
            raw_file=raw_ref,
        )

    async def _transform_entity(
        self,
        *,
        entity_type: str,
        mapper_fn: _MapperFn,
        input: TransformInput,
    ) -> TransformOutput:
        """Read raw JSONL → mapper → transformed JSONL.

        Reads the raw records.json via the ``FileReference`` threaded
        through from the matching ``extract_*`` activity, runs each
        record through ``mapper_fn``, and writes the resulting Atlan
        asset to ``transformed/<entity>/entities.json``.

        Cross-worker contract (BLDX-1281 / PR #1787):
            ``run()`` populates ``input.raw_file`` with the durable
            ``FileReference`` returned by the matching extract activity.
            The activity interceptor materialises that ref onto this
            worker's local FS BEFORE this method runs (SHA-256 sidecar
            verification → fresh local copy even if extract and
            transform landed on different pods). The method below just
            opens ``input.raw_file.local_path`` directly.

            When ``input.raw_file is None`` (extract returned zero rows
            or the caller didn't thread a ref), this is a clean no-op
            returning ``total_record_count=0`` — matches the historical
            contract that the publish step relies on.
        """
        output_path = self._resolve_output_path(input)
        if not output_path:
            return TransformOutput(typename=entity_type, total_record_count=0)

        # Resolve the raw file via the threaded FileReference. The
        # interceptor has already materialised it; we just consume the
        # local_path. Fall back to the legacy local path lookup so
        # callers that don't thread a ref (e.g. unit tests that seed
        # raw files directly under tmp_path) still work.
        if input.raw_file is not None and input.raw_file.local_path:
            raw_file = Path(input.raw_file.local_path)
        else:
            raw_file = Path(output_path) / "raw" / entity_type / "records.json"
        if not raw_file.exists() or raw_file.stat().st_size == 0:
            return TransformOutput(typename=entity_type, total_record_count=0)

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
        with open(raw_file, "rb") as r, open(output_file, "wb") as w:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                record = orjson.loads(line)
                asset = mapper_fn(record, connection_qn)
                # Inject connectionName if the mapper returned a dict
                if isinstance(asset, dict) and connection_name:
                    asset.setdefault("attributes", {}).setdefault(
                        "connectionName", connection_name
                    )
                if hasattr(asset, "to_nested_dict"):
                    payload = asset.to_nested_dict()
                elif hasattr(asset, "model_dump"):
                    payload = asset.model_dump()
                elif isinstance(asset, dict):
                    payload = asset
                else:
                    payload = record
                w.write(orjson.dumps(payload, default=_orjson_default))
                w.write(b"\n")
                count += 1

        logger.info("Transformed %s: %d records", entity_type, count)
        # Emit a FileReference to entities.json so the activity
        # interceptor uploads it after the transform activity finishes.
        # Downstream publish / upload tasks then consume the durable ref
        # via the same materialise contract — no local-FS coupling.
        #
        # See ``_extract_entity`` for why tier=RETAINED — the bare
        # ``file_refs/`` prefix used by the default TRANSIENT tier is
        # rejected (403) by Atlan's blob-storage gateway in production.
        transformed_ref = (
            FileReference.from_local(output_file, tier=StorageTier.RETAINED)
            if count > 0
            else None
        )
        return TransformOutput(
            typename=entity_type,
            total_record_count=count,
            transformed_file=transformed_ref,
        )
