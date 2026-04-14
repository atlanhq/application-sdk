"""SQL metadata extraction App — v3 implementation.

Replaces the v2 ``BaseSQLMetadataExtractionWorkflow`` +
``BaseSQLMetadataExtractionActivities`` split with a single typed ``App`` class.

Subclass ``SqlMetadataExtractor`` to implement connector-specific logic::

    from application_sdk.templates import SqlMetadataExtractor
    from application_sdk.templates.entity import ExtractableEntity

    class MyExtractor(SqlMetadataExtractor):
        entities = [
            ExtractableEntity(task_name="fetch_databases", phase=1),
            ExtractableEntity(task_name="fetch_schemas",   phase=1),
            ExtractableEntity(task_name="fetch_tables",    phase=1),
            ExtractableEntity(task_name="fetch_columns",   phase=1),
            ExtractableEntity(task_name="fetch_stages",    phase=2),
        ]

        @task(timeout_seconds=1800)
        async def fetch_databases(self, input): ...

Or use the legacy class-attribute SQL pattern (still supported)::

    class MyExtractor(SqlMetadataExtractor):
        fetch_database_sql = "SELECT ..."
        fetch_schema_sql   = "SELECT ..."
"""

from __future__ import annotations

from typing import ClassVar

from application_sdk.app.task import task
from application_sdk.common.exc_utils import rewrap
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates.base_metadata_extractor import BaseMetadataExtractor
from application_sdk.templates.contracts.base_metadata_extraction import UploadInput
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
    TransformInput,
    TransformOutput,
)
from application_sdk.templates.entity import (
    ExtractableEntity,
    default_result_key,
    run_entity_phases,
)

logger = get_logger(__name__)

# Default entity definitions — the 4 core SQL entity types.
_DEFAULT_ENTITIES = [
    ExtractableEntity(task_name="fetch_databases", phase=1),
    ExtractableEntity(task_name="fetch_schemas", phase=1),
    ExtractableEntity(task_name="fetch_tables", phase=1),
    ExtractableEntity(task_name="fetch_columns", phase=1),
]

# Maps task_name → (InputClass, OutputClass) for built-in entities.
_ENTITY_CONTRACTS: dict[str, tuple[type[ExtractionTaskInput], type]] = {
    "fetch_databases": (FetchDatabasesInput, FetchDatabasesOutput),
    "fetch_schemas": (FetchSchemasInput, FetchSchemasOutput),
    "fetch_tables": (FetchTablesInput, FetchTablesOutput),
    "fetch_columns": (FetchColumnsInput, FetchColumnsOutput),
    "fetch_procedures": (FetchProceduresInput, FetchProceduresOutput),
}


class SqlMetadataExtractor(BaseMetadataExtractor):
    """Base class for SQL metadata extraction apps.

    Inherits ``upload_to_atlan`` from ``BaseMetadataExtractor``.

    **Entity-driven orchestration:**

    Set the ``entities`` class variable to declare what to extract.
    The ``run()`` method automatically orchestrates all registered
    entities by phase — no need to override ``run()``.

    Entities in the same phase run concurrently.  Phase 2 entities
    start only after all phase 1 entities complete, and so on.

    Each entity's ``task_name`` maps directly to a method on the
    subclass — no naming-convention magic.

    **Legacy class-attribute SQL (still supported):**

    If ``entities`` is not overridden (empty list), the extractor
    falls back to the default 4 entities (databases, schemas, tables,
    columns) and dispatches to the existing ``fetch_*`` task methods.
    """

    entities: ClassVar[list[ExtractableEntity]] = []
    """Override with a list of ``ExtractableEntity`` to declare entities.
    Empty = use defaults (databases, schemas, tables, columns)."""

    # ------------------------------------------------------------------
    # Task methods — override in subclass
    # ------------------------------------------------------------------

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        """Fetch databases from the source system."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_databases()."
        )

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        """Fetch schemas from the source system."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_schemas()."
        )

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
        """Fetch tables from the source system."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_tables()."
        )

    @task(timeout_seconds=1800)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        """Fetch columns from the source system."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_columns()."
        )

    @task(timeout_seconds=1800)
    async def fetch_procedures(
        self, input: FetchProceduresInput
    ) -> FetchProceduresOutput:
        """Fetch stored procedures (optional).

        Not called from the base ``run()``.  Include an
        ``ExtractableEntity(task_name="fetch_procedures")`` in
        ``entities`` if needed.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_procedures()."
        )

    @task(timeout_seconds=1800)
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        """Transform raw extracted data into the target format."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement transform_data()."
        )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def _get_entities(self) -> list[ExtractableEntity]:
        """Return the effective entity list.

        If the subclass sets ``entities``, use that.
        Otherwise fall back to the 4 default entities.
        """
        entities = self.entities if self.entities else _DEFAULT_ENTITIES
        return [e for e in entities if e.enabled]

    async def _fetch_entity(
        self,
        entity: ExtractableEntity,
        base_input: ExtractionInput,
    ) -> tuple[str, int]:
        """Dispatch to the task method specified by ``entity.task_name``.

        Returns:
            Tuple of (result_key, record_count).
        """
        method = getattr(self, entity.task_name, None)
        if method is None:
            raise NotImplementedError(
                f"{type(self).__name__} has no '{entity.task_name}' method "
                f"for entity with task_name='{entity.task_name}'."
            )

        # Build the typed task input
        input_cls = _ENTITY_CONTRACTS.get(
            entity.task_name, (ExtractionTaskInput, None)
        )[0]

        # Prefer credential_ref; fall back to legacy credential_guid
        cred_ref = base_input.credential_ref
        if cred_ref is None and base_input.credential_guid:
            from application_sdk.credentials import legacy_credential_ref

            cred_ref = legacy_credential_ref(base_input.credential_guid)

        task_input = input_cls(
            workflow_id=base_input.workflow_id,
            connection=base_input.connection,
            credential_guid=base_input.credential_guid,
            credential_ref=cred_ref,
            output_prefix=base_input.output_prefix,
            output_path=base_input.output_path,
            exclude_filter=base_input.exclude_filter,
            include_filter=base_input.include_filter,
            temp_table_regex=base_input.temp_table_regex,
            source_tag_prefix=base_input.source_tag_prefix,
        )

        result = await method(task_input)
        result_key = entity.result_key or default_result_key(entity.task_name)
        count = getattr(result, "total_record_count", 0)
        return (result_key, count)

    async def run(self, input: ExtractionInput) -> ExtractionOutput:  # type: ignore[override]
        """Orchestrate the full metadata extraction pipeline.

        Executes entities grouped by phase.  All entities in the same
        phase run concurrently.  Phase N+1 starts only after phase N
        completes.  After extraction, uploads results to Atlan.
        """
        workflow_id = input.workflow_id
        logger.info("Starting SQL metadata extraction: %s", workflow_id)

        try:
            entities = self._get_entities()
            results = await run_entity_phases(self, entities, input)

            logger.info(
                "Metadata extraction completed",
                workflow_id=workflow_id,
                results=results,
            )

            # Upload extracted data to Atlan
            if input.output_path:
                upload_result = await self.upload_to_atlan(
                    UploadInput(output_path=input.output_path)
                )
                records_uploaded = upload_result.migrated_files
            else:
                records_uploaded = 0

            # Build output dynamically — any result key matching an
            # ExtractionOutput field gets populated automatically.
            output_fields = ExtractionOutput.model_fields
            entity_counts = {}
            for k, v in results.items():
                if k in output_fields:
                    entity_counts[k] = v
                else:
                    logger.warning(
                        "Result key '%s' has no matching field in %s — "
                        "define the field on the output class or set "
                        "result_key explicitly.",
                        k,
                        ExtractionOutput.__name__,
                    )

            return ExtractionOutput(
                workflow_id=workflow_id,
                success=True,
                records_uploaded=records_uploaded,
                **entity_counts,
            )

        except Exception as e:
            raise rewrap(
                e, f"SQL metadata extraction failed (workflow_id={workflow_id})"
            ) from e
