"""SqlApp — consolidated SQL metadata extraction template.

Replaces ``SqlMetadataExtractor``, ``SqlQueryExtractor``,
``IncrementalSqlMetadataExtractor`` (with a custom ``run()``), and
``BaseMetadataExtractor`` (for SQL sources; a non-SQL connector subclasses
``application_sdk.app.App``) with a single App class that provides:

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
* Per-entity ``FileReference`` emission with pre-set canonical
  ``storage_path`` keys (``<run_prefix>/raw/<entity>/records.json`` /
  ``<run_prefix>/transformed/<entity>/entities.json``) — the activity
  interceptor's persist step uploads each one to that exact key, so
  downstream publish discovers the data at the path it expects with
  no separate upload step needed (BLDX-1281).
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
            # Every required value is URL-encoded (host and port are
            # validated) before it fills the template; pass raw values.
            required=["username", "password", "host", "port", "database"],
        )

    class MySQLApp(SqlApp):
        sql_client_class = MySQLClient

        fetch_database_sql = "SELECT SCHEMA_NAME as database_name FROM ..."
        fetch_schema_sql = "SELECT SCHEMA_NAME as schema_name FROM ..."
        fetch_table_sql = "SELECT TABLE_NAME as table_name FROM ..."
        fetch_column_sql = "SELECT COLUMN_NAME as column_name FROM ..."

        def map_table(self, record, connection_qn):
            from pyatlan_v9.model.assets import Database, Schema, Table

            # Build assets with the pyatlan_v9 ``.creator()`` factories: each one
            # computes its own qualifiedName from its parent's, so the grammar
            # lives in pyatlan, not in a hand-rolled ``f"{connection_qn}/..."``
            # string per connector (which conformance P028 flags).
            database = Database.creator(
                name=record["database_name"],
                connection_qualified_name=connection_qn,
            )
            schema = Schema.creator(
                name=record["schema_name"],
                database_qualified_name=database.qualified_name,
            )
            return Table.creator(
                name=record["table_name"],
                schema_qualified_name=schema.qualified_name,
            )

        def map_column(self, record, connection_qn):
            from pyatlan_v9.model.assets import Column
            return Column(...)
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from temporalio import workflow as _temporal_workflow
from typing_extensions import deprecated

# The names marked ``noqa: F401`` below are used by the mixins this class now
# inherits (FND-4267), not here, but were importable from this module in past
# releases, so they stay bound (docs/standards/symbols.md).
from application_sdk import _deprecation
from application_sdk._runtime.offload import raise_if_offload_abandoned, run_in_thread  # noqa: F401
from application_sdk._runtime.progress import current_progress_tracker  # noqa: F401
from application_sdk.app._app_config_validation import warn_config_problem
from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry
from application_sdk.app.task import task
from application_sdk.clients._sql_failure_classifier import (
    SqlFailureKind,  # noqa: F401
    classify_sql_failure,  # noqa: F401
    exception_class_names,  # noqa: F401
    root_cause,
)
from application_sdk.common.asset_serialization import entity_bytes  # noqa: F401
from application_sdk.common.asset_serialization import orjson_default as _orjson_default  # noqa: F401
from application_sdk.common.entity_envelope import (
    DEFAULT_ENVELOPE,
    EntityDecorations,
    EntityEnvelopePolicy,
)
from application_sdk.common.last_sync import resolve_last_sync_details  # noqa: F401
from application_sdk.common.sql_filters import (
    normalize_filters,  # noqa: F401
    safe_substitute_placeholders,  # noqa: F401
    strip_sql_block_comments,  # noqa: F401
)
from application_sdk.constants import TEMPORARY_PATH as _TEMPORARY_PATH
from application_sdk.constants import _run_prefix
from application_sdk.contracts.base import OutputStatus  # noqa: F401
from application_sdk.contracts.storage import VerifyRefsInput  # noqa: F401
from application_sdk.contracts.types import FileReference, StorageTier  # noqa: F401
from application_sdk.credentials import CredentialResolver  # noqa: F401
from application_sdk.credentials.ref import CredentialRef
from application_sdk.errors import (
    AppError,  # noqa: F401
    redact_secrets,  # noqa: F401
    safe_traceback,  # noqa: F401
)

# ``AppTimeoutError`` is re-exported: importable from here before FND-4687 moved
# the wire-envelope rebuild into ``errors.wire`` (Symbol Removal Check).
from application_sdk.errors.leaves import AppTimeoutError as AppTimeoutError
from application_sdk.errors.leaves import (
    AuthError,  # noqa: F401
    InternalError,  # noqa: F401
    SourceUnavailableError,  # noqa: F401
)
from application_sdk.errors.wire import (
    FailureDetails,  # noqa: F401
    degraded_failure_details,  # noqa: F401
    error_from_failure_details,  # noqa: F401
    redact_evidence_value,  # noqa: F401
    redacted_validation_reason,  # noqa: F401
    register_wire_leaf,  # noqa: F401
)
from application_sdk.execution import build_output_path, get_object_store_prefix  # noqa: F401
from application_sdk.infrastructure.context import get_infrastructure  # noqa: F401
from application_sdk.observability.logger_adaptor import get_logger

# The prime task, the per-entity I/O and the run()-override helpers are
# mixins (FND-4267); ``SqlApp`` inherits them, so every name is still
# ``SqlApp.<name>``. ``_EXTRACT_BATCH_SIZE`` and ``_MapperFn`` are bound here
# under the names this module has always had.
from application_sdk.templates._sql_app_entity import (
    _EXTRACT_BATCH_SIZE,  # noqa: F401 — importable from here in past releases
    _MapperFn,
)
from application_sdk.templates._sql_app_finalize import _SqlFinalizeMixin
from application_sdk.templates._sql_app_prime import _SqlPrimeAuthMixin
from application_sdk.templates._template_errors import (
    SqlTreeFilterInputUnsupportedError,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionTaskInput,
    ExtractionTaskOutput,
    PrimeAuthOutput,  # noqa: F401
    TransformInput,
    TransformOutput,
    TreeSelectionExtractionInput,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionOutput as _ExtractionOutput,
)
from application_sdk.templates.sql_app_errors import (
    SqlProbeTimeoutError,  # noqa: F401
    TransformedFileMissingError,  # noqa: F401
)

if TYPE_CHECKING:
    from pyatlan_v9.model.assets import Asset

    from application_sdk.clients.sql import BaseSQLClient

__all__ = [
    "TEMPORARY_PATH",
    "ExtractionOutput",
    "SqlApp",
]


logger = get_logger(__name__)

_ET = TypeVar("_ET", bound=ExtractionTaskInput)

# Re-exported under its historical private name; the one implementation lives
# with the shared SQL failure classifier.
_root_cause = root_cause


def _check_sql_client_class(cls: type[SqlApp]) -> None:
    """Warn at class definition when a concrete ``SqlApp`` has no usable client.

    ``_init_sql_client`` checks ``sql_client_class`` only inside a running
    task, so a subclass that forgets it otherwise fails in the worker, not at
    import or in unit tests. Skipped for a template (``_app_registered`` set
    on the class itself), an abstract base, and a class that overrides
    ``_init_sql_client`` and so never reads the attribute. Warn-only until
    ``APP_CONFIG_ERROR_VERSION``.
    """
    if (
        cls.__dict__.get("_app_registered", False)
        or _temporal_workflow.unsafe.in_sandbox()
    ):
        return
    # ABCMeta fills __abstractmethods__ only after __init_subclass__ runs, so
    # scan the way App.__init_subclass__ does.
    if any(
        getattr(getattr(cls, m, None), "__isabstractmethod__", False) for m in dir(cls)
    ):
        return
    if cls._init_sql_client is not SqlApp._init_sql_client:
        return
    client_class = cls.sql_client_class
    if client_class is None:
        problem = (
            "on a concrete SqlApp means every extract task fails in the "
            "worker with SqlClientClassNotSetError; set it to your "
            "BaseSQLClient subclass"
        )
    else:
        from application_sdk.clients.sql import (  # noqa: PLC0415 — only for a declared client; keeps template import light
            BaseSQLClient,
        )

        if isinstance(client_class, type) and issubclass(client_class, BaseSQLClient):
            return
        problem = "is not a BaseSQLClient subclass"
    warn_config_problem(cls, "sql_client_class", client_class, problem)


class SqlApp(_SqlPrimeAuthMixin, _SqlFinalizeMixin, App):
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

    **The ``run()``-override surface.** Overriding ``run()`` is a documented
    path, so everything an override needs to wire the pieces together is
    public. Reaching for anything underscore-prefixed here means the surface
    has a gap — raise it rather than working around it:

    - ``extract_*`` / ``transform_*`` — the per-entity tasks themselves.
    - :meth:`resolve_credential_ref` — produces the ``cred_ref`` that
      :meth:`build_task_input` needs.
    - :meth:`build_task_input` — builds a typed task input from the
      top-level ``ExtractionInput``.
    - :meth:`build_transform_input` — threads an extract's ``raw_file`` ref
      into the matching transform's input.
    - :meth:`collect_transformed_files` — turns the transforms' outputs into
      the declaration ``App.verify_refs`` / ``App.upload_refs`` check
      against. An override that adds an entity the default ``run()`` does
      not drive must fold its refs in, or that entity's assets are dropped
      from the upload silently.
    - :meth:`finalize_extraction` — does that folding **and** verifies the
      combined declaration. Prefer it to concatenating by hand: it is the
      only way the extra entity gets asserted, because ``super().run()``
      verifies the four refs it drove and then returns.

    An override that adds a fifth entity therefore looks like::

        async def run(self, input: ExtractionInput) -> ExtractionOutput:
            base = await super().run(input)
            cred_ref = self.resolve_credential_ref(input)
            task_input = self.build_task_input(
                ExtractionTaskInput, input, cred_ref=cred_ref
            )
            proc = await self.extract_procedures(task_input)
            out = await self.transform_procedures(
                self.build_transform_input(task_input, proc.raw_file)
            )
            return await self.finalize_extraction(base, [out])

    A connector that also has to bridge the tree across an SDR store boundary
    passes the finalised declaration to ``App.upload_refs`` — which verifies
    its own delivery — and returns the prefix that comes back.

    An override that does not call ``super().run()`` must end in
    :meth:`finalize_extraction`: it is the only thing that checks the
    transformed tree is whole before publish diffs the tenant against it.
    Conformance rule P068 flags an override that does neither.

    **The per-entity surface.** Below ``run()``, these are the supported
    extension points; everything else is implementation:

    - ``sql_client_class`` and the ``fetch_*_sql`` templates — set them.
    - ``map_*`` — override; the base raises for the ones you have not
      written.
    - ``extract_*`` / ``transform_*`` — override to change one entity's SQL
      or mapper, or to split it across targets.
    - :meth:`extract_entity` / :meth:`transform_entity` — call them from those
      overrides. They own the streaming, the event-loop offload, the progress
      marks and the canonical ``FileReference`` keys; re-implementing them is
      how a connector's output ends up where publish does not look.
    - :meth:`resolve_output_path` — the directory those two write under, for
      a connector writing files of its own beside them.
    - ``entity_envelope`` and :meth:`decorate_entity` — declare how the
      finished line is shaped. No connector overrides ``decorate_entity``
      today; it exists so the first one that owes a downstream app a
      root-level field adds it to ``EntityDecorations`` instead of
      post-processing the line.

    Some connectors also override ``_init_sql_client`` or ``_prepare_sql``.
    Those are not yet promoted to this surface, so a rename can still reach
    them; raise it before adding another override of an underscore name.
    """

    _app_registered: ClassVar[bool] = True  # abstract template, not concrete

    # ── SQL client ──────────────────────────────────────────────────────
    sql_client_class: ClassVar[type[BaseSQLClient] | None] = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        # Before super(): App registration sets _app_registered on cls,
        # which would make every concrete app read as a template.
        _check_sql_client_class(cls)
        super().__init_subclass__(**kwargs)

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

    # ── Transformed-output envelope (FND-2137) ──────────────────────────
    #: How this connector's serialised entities are shaped. Declared once per
    #: app, not per mapper: the envelope is a property of the downstream
    #: contract, and per-mapper choice is how the fleet ended up with four
    #: mutually incompatible ones. The default is flattened — see
    #: :mod:`application_sdk.common.entity_envelope` for why, and for the
    #: migration lever a connector uses when its released output is the
    #: pyatlan-native shape.
    entity_envelope: ClassVar[EntityEnvelopePolicy] = DEFAULT_ENVELOPE

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
        if isinstance(src, TreeSelectionExtractionInput):
            raise SqlTreeFilterInputUnsupportedError()
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
    # @task: SQL auth cache pre-warm — Argo parity (BLDX-1295)
    # =====================================================================

    # =====================================================================
    # prime_sql_auth failure classifier (BLDX-1295)
    # =====================================================================

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
    #
    # The ``extract_*`` / ``transform_*`` tasks declare no timing values on
    # purpose: their runtime scales with source size, so any number here is a
    # guess (ADR-0018). They take the process-wide defaults —
    # ``ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS`` (24 h backstop) and
    # ``ATLAN_HEARTBEAT_TIMEOUT_SECONDS`` — which operators tune per app.
    # tests/unit/templates/test_template_task_literals.py pins this.

    @task()
    async def extract_databases(
        self, input: ExtractionTaskInput
    ) -> ExtractionTaskOutput:
        """Stream database/catalog rows from SQL into raw JSONL."""
        return await self.extract_entity(
            entity_type="database",
            sql_template=self.fetch_database_sql,
            input=input,
        )

    @task()
    async def extract_schemas(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream schema rows from SQL into raw JSONL."""
        return await self.extract_entity(
            entity_type="schema",
            sql_template=self.fetch_schema_sql,
            input=input,
        )

    @task()
    async def extract_tables(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream table rows from SQL into raw JSONL."""
        return await self.extract_entity(
            entity_type="table",
            sql_template=self.fetch_table_sql,
            input=input,
        )

    @task()
    async def extract_columns(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream column rows from SQL into raw JSONL."""
        return await self.extract_entity(
            entity_type="column",
            sql_template=self.fetch_column_sql,
            input=input,
        )

    @task()
    async def extract_views(self, input: ExtractionTaskInput) -> ExtractionTaskOutput:
        """Stream view rows from SQL into raw JSONL."""
        return await self.extract_entity(
            entity_type="view",
            sql_template=self.fetch_view_sql,
            input=input,
        )

    @task()
    async def extract_procedures(
        self, input: ExtractionTaskInput
    ) -> ExtractionTaskOutput:
        """Stream stored-procedure rows from SQL into raw JSONL.

        Writes to the ``extras-procedure`` entity type so the publish-app
        can parse the SQL ``definition`` field and derive lineage
        (Process / ColumnProcess entities) — matches the legacy Argo
        crawler output layout.
        """
        return await self.extract_entity(
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

    @task()
    async def transform_databases(self, input: TransformInput) -> TransformOutput:
        """Map raw database records to Atlan assets via ``map_database``."""
        return await self.transform_entity(
            entity_type="database",
            mapper_fn=self.map_database,
            input=input,
        )

    @task()
    async def transform_schemas(self, input: TransformInput) -> TransformOutput:
        """Map raw schema records to Atlan assets via ``map_schema``."""
        return await self.transform_entity(
            entity_type="schema",
            mapper_fn=self.map_schema,
            input=input,
        )

    @task()
    async def transform_tables(self, input: TransformInput) -> TransformOutput:
        """Map raw table records to Atlan assets via ``map_table``."""
        return await self.transform_entity(
            entity_type="table",
            mapper_fn=self.map_table,
            input=input,
        )

    @task()
    async def transform_columns(self, input: TransformInput) -> TransformOutput:
        """Map raw column records to Atlan assets via ``map_column``."""
        return await self.transform_entity(
            entity_type="column",
            mapper_fn=self.map_column,
            input=input,
        )

    @task()
    async def transform_views(self, input: TransformInput) -> TransformOutput:
        """Map raw view records to Atlan assets via ``map_table``.

        Views go through ``map_table`` because Atlan models View as a
        Table specialisation (typeName ``View``) — connectors differentiate
        via the ``table_kind`` field on the record.
        """
        return await self.transform_entity(
            entity_type="view",
            mapper_fn=self.map_table,
            input=input,
        )

    @task()
    async def transform_procedures(self, input: TransformInput) -> TransformOutput:
        """Map raw procedure records to Atlan assets via ``map_procedure``."""
        return await self.transform_entity(
            entity_type="extras-procedure",
            mapper_fn=self.map_procedure,
            input=input,
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

    def decorate_entity(
        self, *, entity_type: str, record: dict[str, Any]
    ) -> EntityDecorations | None:
        """Top-level contract fields for one record. Override if your
        connector owes a downstream app a field pyatlan cannot hold.

        These land on the entity *root*, beside ``typeName`` — not in
        ``attributes`` — because that is where their readers look. See
        :class:`~application_sdk.common.entity_envelope.EntityDecorations` for
        the fields and who reads each one.

        The return type is a typed model, not a dict, deliberately: a
        decoration is a named cross-app contract, and a free dict is how a
        second undocumented side-channel gets added without anyone noticing.
        A connector that needs a field this class does not have adds it there,
        which forces the conversation about who reads it.

        Args:
            entity_type: The stream being transformed (``"table"``,
                ``"column"``, …) — the ``transformed/<entity>/`` the line
                lands in, not the Atlas type. A mapper can return a ``View``
                into the ``table`` stream.
            record: The raw source row the asset was mapped from.

        Returns:
            The decorations for this record, or ``None`` (the default) for a
            connector that owes none.
        """
        return None

    # =====================================================================
    # run() — default orchestration
    # =====================================================================

    async def run(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, input: ExtractionInput
    ) -> _ExtractionOutput:
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
        cred_ref = self.resolve_credential_ref(input)

        task_input = self.build_task_input(
            ExtractionTaskInput, input, cred_ref=cred_ref
        )

        # ── Phase 0: Pre-warm SQL auth cache (BLDX-1295) ────────────────
        # Argo parity fix. One serial probe connection BEFORE the
        # parallel ``extract_*`` burst so MySQL 8's
        # ``caching_sha2_password`` server-side cache is populated. The
        # subsequent parallel connections then take the fast-auth path
        # and never trip ``FAILED_LOGIN_ATTEMPTS`` lockouts.
        #
        # Failure handling: prime_sql_auth catches probe exceptions and
        # returns them as ``success=False`` on its output, carrying the
        # verdict it classified against the live exception. We rebuild that
        # typed error and raise it, so operators get actionable guidance for
        # the *actual* failure mode (not a one-size-fits-all auth-lockout
        # message — which would mislead an on-call investigating a DNS or TLS
        # misconfiguration). The parallel extract burst still never
        # runs on prime failure regardless of which error type we raise.
        prime_result = await self.prime_sql_auth(task_input)
        if not prime_result.success:
            raise self._classify_prime_failure(prime_result)

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
        #
        # The gather's results are kept, not discarded (FND-1790). Each
        # ``TransformOutput`` carries the ``transformed_file``
        # ``FileReference`` its task wrote — together they are the only
        # record this run has of what the transform step produced, and
        # therefore the only thing ``transformed_data_prefix`` can be
        # checked against before it is handed on.
        transform_results = await asyncio.gather(
            self.transform_databases(
                self.build_transform_input(task_input, db_result.raw_file)
            ),
            self.transform_schemas(
                self.build_transform_input(task_input, schema_result.raw_file)
            ),
            self.transform_tables(
                self.build_transform_input(task_input, table_result.raw_file)
            ),
            self.transform_columns(
                self.build_transform_input(task_input, column_result.raw_file)
            ),
        )

        # ── Phase 3: ExtractionOutput ───────────────────────────────────
        # No explicit upload step — every ``raw_file`` / ``transformed_file``
        # ``FileReference`` emitted by the extract/transform tasks above
        # carries a pre-set canonical ``storage_path``
        # (``<run_prefix>/raw/<entity>/records.json`` /
        # ``<run_prefix>/transformed/<entity>/entities.json``), and the
        # activity interceptor's persist step has already uploaded each
        # one to that key. Publish discovers transformed assets by
        # walking ``transformed/<entity>/`` prefixes, so all the data
        # the downstream pipeline needs is already in object store
        # before this method returns. Subclasses that produce
        # side-files outside the FileReference contract should issue
        # their own ``App.upload(...)`` call (or override ``run()`` to
        # do so) — the template no longer auto-walks ``output_path``
        # because the walk was redundant with the canonical-key
        # uploads and didn't cross pod boundaries safely (BLDX-1281).

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
                _TEMPORARY_PATH,
                _run_prefix(
                    AppRegistry.resolve_running_app_name(prefer=self._app_name),
                    info.workflow_id,
                    info.run_id,
                ),
            )

        transformed_data_prefix = get_object_store_prefix(
            os.path.join(resolved_base, "transformed")
        )

        # -- Phase 4: Verify the handoff against the producer's declaration --
        # ``transformed_data_prefix`` is publish's contract and stays exactly
        # as it is - publish walking it is correct behaviour. What was missing
        # is any way for *this* method to know the prefix it names is whole.
        # A walk cannot tell "absent" from "lost": a transformed/ tree short by
        # one entity looks identical to a run that only had three, and publish
        # then diffs the tenant against the subset and archives the rest
        # (APP-CORRECTNESS-001).
        #
        # ``finalize_extraction`` collects the transforms' refs into the
        # expected set and asserts every declared object is present under the
        # prefix about to be returned, so a hole fails the run here instead of
        # surfacing as a short publish two stages downstream.
        #
        # The default path goes through the same method a ``run()`` override
        # calls, deliberately: the two cannot drift, and an override adding a
        # fifth entity gets the identical assertion rather than a recipe it has
        # to reproduce correctly.
        base = _ExtractionOutput(
            databases_extracted=db_result.total_record_count,
            schemas_extracted=schema_result.total_record_count,
            tables_extracted=table_result.total_record_count,
            columns_extracted=column_result.total_record_count,
            connection_qualified_name=connection_qn,
            transformed_data_prefix=transformed_data_prefix,
            # Expose the resolved local base path so subclasses can derive
            # additional prefixes (e.g. lineage-specific dirs) without calling
            # workflow.info() a second time.
            output_path=resolved_base,
        )
        # ``transformed_files`` on the result is the producer's declaration of
        # what it wrote, surfaced so a connector's Atlan bridge can upload each
        # ref by reference instead of scanning a directory it may not share.
        return await self.finalize_extraction(base, transform_results)

    # =====================================================================
    # Public helpers for ``run()`` overrides
    # =====================================================================

    def resolve_credential_ref(self, input: ExtractionInput) -> CredentialRef | None:
        """Resolve credential ref from extraction input.

        Part of the supported ``run()``-override surface (see the class
        docstring). :meth:`build_task_input` takes a ``cred_ref`` and cannot be
        called correctly without one, so a public builder needing a privately
        produced argument left the documented path unusable as documented.

        Call this rather than :meth:`CredentialRef.resolve_or_none` directly.
        Both return the same value today, but this method is the SDK's routing
        seam and is shared with the injected preflight gate — going through it
        is what keeps an override and the gate resolving identically if the
        routing ever changes.

        Delegates to :meth:`CredentialRef.resolve_or_none` — prefers the input's
        own ``credential_ref``, routes direct (credential_guid) / agent
        (agent_json) modes, and degrades to a GUID ref or ``None`` on a
        routing edge case.

        When the input names no credential at all (no GUID, pre-built ref,
        declared named refs or agent signal), falls back to the connection's
        ``defaultCredentialGuid``. The eligibility rule is the one the injected
        gate uses, so a miner input that reuses a crawler's connection resolves
        the same credential in both places. Before this fallback such an input
        returned ``None`` here.
        """
        ref = CredentialRef.resolve_or_none(input)
        if ref is not None:
            return ref
        from application_sdk.credentials.ref import (  # noqa: PLC0415
            _guid_credential_ref,
        )
        from application_sdk.credentials.routing import (  # noqa: PLC0415
            _connection_credential_fallback_guid,
        )

        guid = _connection_credential_fallback_guid(input)
        return _guid_credential_ref(guid) if guid else None

    # =====================================================================
    # Deprecated private aliases — remove in v4.0.0
    # =====================================================================
    #
    # These were private, so they carried no compatibility promise — but a
    # `gh search code` for ``self._resolve_credential_ref`` over the atlanhq org
    # finds call sites in 7 connector repos (cloudera-impala, db2, mssql, mysql,
    # presto, sapase, sapdatasphere) and 4 for ``self._build_transform_input``
    # (db2, mssql app + miner, mysql, sapase). Searching the bare names matches
    # 12 and 4, but the extras are docstring cross-references and design docs —
    # worth stating precisely, because "12 repos break" and "7 repos break" are
    # the same decision only until someone re-derives the number.
    # A rename with no shim breaks every one of them on their next SDK bump,
    # which is a fleet-wide outage traded for a tidier diff. They delegate, so
    # there is exactly one implementation either way.
    #
    # ``_extract_entity``, ``_transform_entity`` and ``_resolve_output_path``
    # joined them in FND-4686. Of the seven connectors that subclass SqlApp,
    # app code in db2, mssql and sapase calls ``_transform_entity``, db2 and
    # teradata call ``_extract_entity``, and db2, mssql (app + miner) and
    # sapase call ``_resolve_output_path``; clickhouse and db2 tests call
    # ``_transform_entity`` directly. That is the surface they actually build
    # on. None of the seven overrides any of the three, so delegating shims
    # keep every caller working unchanged.
    #
    # What the shims do NOT save: a test that patches one of these names. The
    # patch lands on the shim while every SDK-internal caller now goes to the
    # public name, so the mock is never reached — quieter than a broken import
    # and worth knowing before debugging one. Each docstring says so.

    @deprecated(
        "SqlApp._resolve_credential_ref is deprecated; use the public "
        "SqlApp.resolve_credential_ref instead. Will be removed in v4.0.0."
    )
    def _resolve_credential_ref(self, input: ExtractionInput) -> CredentialRef | None:
        """**Deprecated** — use :meth:`resolve_credential_ref`.

        Removed in v4.0.0.

        .. warning::
            **Patching this name no longer intercepts the SDK.** ``run()`` and
            the preflight gate call :meth:`resolve_credential_ref` now, so a
            ``patch.object(SqlApp, "_resolve_credential_ref", ...)`` in a
            connector's tests still patches *something* — this shim — and is
            simply never reached. Depending on what the test asserts that
            surfaces as a failed call-count, or as the real implementation
            running under a mock the author believes is in place. Move the
            patch site to the public name.
        """
        return self.resolve_credential_ref(input)

    @staticmethod
    @deprecated(
        "SqlApp._build_transform_input is deprecated; use the public "
        "SqlApp.build_transform_input instead. Will be removed in v4.0.0."
    )
    def _build_transform_input(
        base: ExtractionTaskInput,
        raw_file: FileReference | None,
    ) -> TransformInput:
        """**Deprecated** — use :meth:`build_transform_input`.

        Removed in v4.0.0.

        .. warning::
            **Patching this name no longer intercepts the SDK** — see
            :meth:`_resolve_credential_ref` for why. Move the patch site to
            :meth:`build_transform_input`.
        """
        return SqlApp.build_transform_input(base, raw_file)

    @staticmethod
    @deprecated(
        "SqlApp._resolve_output_path is deprecated; use the public "
        "SqlApp.resolve_output_path instead. Will be removed in v4.0.0."
    )
    def _resolve_output_path(input: ExtractionTaskInput) -> str:
        """**Deprecated** — use :meth:`resolve_output_path`.

        Removed in v4.0.0.

        .. warning::
            **Patching this name no longer intercepts the SDK** — see
            :meth:`_resolve_credential_ref` for why. Move the patch site to
            :meth:`resolve_output_path`.
        """
        return SqlApp.resolve_output_path(input)

    @deprecated(
        "SqlApp._extract_entity is deprecated; use the public "
        "SqlApp.extract_entity instead. Will be removed in v4.0.0."
    )
    async def _extract_entity(
        self,
        *,
        entity_type: str,
        sql_template: str,
        input: ExtractionTaskInput,
    ) -> ExtractionTaskOutput:
        """**Deprecated** — use :meth:`extract_entity`.

        Removed in v4.0.0.

        .. warning::
            **Patching or overriding this name no longer intercepts the SDK**
            — see :meth:`_resolve_credential_ref` for why. Move the patch site
            or the override to :meth:`extract_entity`.
        """
        return await self.extract_entity(
            entity_type=entity_type, sql_template=sql_template, input=input
        )

    @deprecated(
        "SqlApp._transform_entity is deprecated; use the public "
        "SqlApp.transform_entity instead. Will be removed in v4.0.0."
    )
    async def _transform_entity(
        self,
        *,
        entity_type: str,
        mapper_fn: _MapperFn,
        input: TransformInput,
    ) -> TransformOutput:
        """**Deprecated** — use :meth:`transform_entity`.

        Removed in v4.0.0.

        .. warning::
            **Patching or overriding this name no longer intercepts the SDK**
            — see :meth:`_resolve_credential_ref` for why. Move the patch site
            or the override to :meth:`transform_entity`.
        """
        return await self.transform_entity(
            entity_type=entity_type, mapper_fn=mapper_fn, input=input
        )

    # =====================================================================
    # Per-entity engine — extract_entity / transform_entity
    # =====================================================================
    #
    # Public because they are what connectors build on: every SQL app that
    # adds an entity, splits a source across targets, or runs flavour-specific
    # mappers calls these from its own extract_* / transform_* tasks rather
    # than re-implementing the streaming, the offload and the FileReference
    # pinning below.


if TYPE_CHECKING:
    # Served at runtime by the deprecation __getattr__ below; bound here so
    # type checkers see the real type and __all__ names a defined symbol.
    from application_sdk.constants import TEMPORARY_PATH
    from application_sdk.templates.contracts.sql_metadata import ExtractionOutput

# ── Deprecated re-exports ────────────────────────────────────────────────────
#: name -> (replacement, why). This module imported ``legacy_credential_ref``
#: until FND-4691 switched it to the private GUID constructor, which left the
#: name importable from here only by accident of that import. Served once more
#: by ``__getattr__`` below (PEP 562) so the removal waits for v4.0.0.
#: ``WORKFLOW_OUTPUT_PATH_TEMPLATE`` likewise, since FND-4921 composes the run
#: prefix through ``application_sdk.constants._run_prefix``.
#: FND-4687 moved the ``FailureDetails`` → ``AppError`` rebuild into
#: ``application_sdk.errors.wire``; the names it imported for that, and the
#: rebuild itself (which a connector imports), are served from there.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "ExtractionOutput": (
        "application_sdk.templates.contracts.sql_metadata.ExtractionOutput",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "TEMPORARY_PATH": (
        "application_sdk.constants.TEMPORARY_PATH",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "_error_from_failure_details": (
        "application_sdk.errors.wire.error_from_failure_details",
        "the FailureDetails rebuild moved to errors.wire so non-SQL apps can use it",
    ),
    "_redacted_validation_reason": (
        "application_sdk.errors.wire.redacted_validation_reason",
        "the FailureDetails rebuild moved to errors.wire so non-SQL apps can use it",
    ),
    "Audience": (
        "application_sdk.errors.categories.Audience",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "FailureCategory": (
        "application_sdk.errors.categories.FailureCategory",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "redact_wire_value": (
        "application_sdk.errors.base.redact_wire_value",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "sanitize_cause_repr": (
        "application_sdk.errors.base.sanitize_cause_repr",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "secret_named_evidence_keys": (
        "application_sdk.errors.wire.secret_named_evidence_keys",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "legacy_credential_ref": (
        "application_sdk.credentials.ref.legacy_credential_ref",
        "it was only ever re-exported here as a side effect of an import",
    ),
    "WORKFLOW_OUTPUT_PATH_TEMPLATE": (
        "application_sdk.constants.WORKFLOW_OUTPUT_PATH_TEMPLATE",
        "it was only ever re-exported here as a side effect of an import",
    ),
}


__getattr__ = _deprecation.deprecated_module_getattr(
    __name__, _DEPRECATED_CONSTANTS, remove_in="4.0.0"
)
