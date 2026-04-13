"""SQL metadata extraction App — v3 implementation.

Replaces the v2 ``BaseSQLMetadataExtractionWorkflow`` +
``BaseSQLMetadataExtractionActivities`` split with a single typed ``App`` class.

Subclass ``SqlMetadataExtractor`` to implement connector-specific logic::

    from application_sdk.templates import SqlMetadataExtractor
    from application_sdk.templates.entity import EntityDef

    class MyExtractor(SqlMetadataExtractor):
        entities = [
            EntityDef(name="databases", phase=1),
            EntityDef(name="schemas",   phase=1),
            EntityDef(name="tables",    phase=1),
            EntityDef(name="columns",   phase=1),
            EntityDef(name="stages",    sql="SELECT ...", phase=2),
        ]

        @task(timeout_seconds=1800)
        async def fetch_databases(self, input): ...

Or use the legacy class-attribute SQL pattern (still supported)::

    class MyExtractor(SqlMetadataExtractor):
        fetch_database_sql = "SELECT ..."
        fetch_schema_sql   = "SELECT ..."
"""

from __future__ import annotations

import asyncio
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
from application_sdk.templates.entity import EntityDef

logger = get_logger(__name__)

# Default entity definitions — the 4 core SQL entity types.
_DEFAULT_ENTITIES = [
    EntityDef(name="databases", phase=1),
    EntityDef(name="schemas", phase=1),
    EntityDef(name="tables", phase=1),
    EntityDef(name="columns", phase=1),
]

# Maps entity name → (InputClass, OutputClass) for built-in entities.
_ENTITY_CONTRACTS: dict[str, tuple[type[ExtractionTaskInput], type]] = {
    "databases": (FetchDatabasesInput, FetchDatabasesOutput),
    "schemas": (FetchSchemasInput, FetchSchemasOutput),
    "tables": (FetchTablesInput, FetchTablesOutput),
    "columns": (FetchColumnsInput, FetchColumnsOutput),
    "procedures": (FetchProceduresInput, FetchProceduresOutput),
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

    For each entity, ``run()`` dispatches to ``fetch_{name}()`` if
    a ``@task`` method with that name exists on the subclass.

    **Legacy class-attribute SQL (still supported):**

    If ``entities`` is not overridden (empty list), the extractor
    falls back to the default 4 entities (databases, schemas, tables,
    columns) and dispatches to the existing ``fetch_*`` task methods.
    """

    entities: ClassVar[list[EntityDef]] = []
    """Override with a list of ``EntityDef`` to declare entities.
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

        Not called from the base ``run()``.  Include an ``EntityDef``
        with ``name="procedures"`` in ``entities`` if needed.
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

    def _get_entities(self) -> list[EntityDef]:
        """Return the effective entity list.

        If the subclass sets ``entities``, use that.
        Otherwise fall back to the 4 default entities.
        """
        entities = self.entities if self.entities else _DEFAULT_ENTITIES
        return [e for e in entities if e.enabled]

    async def _fetch_entity(
        self,
        entity: EntityDef,
        base_input: ExtractionInput,
    ) -> tuple[str, int]:
        """Dispatch to the correct ``fetch_{name}()`` task method.

        Returns:
            Tuple of (result_key, record_count).
        """
        method_name = f"fetch_{entity.name}"
        method = getattr(self, method_name, None)
        if method is None:
            raise NotImplementedError(
                f"{type(self).__name__} has no fetch_{entity.name}() method "
                f"for entity '{entity.name}'."
            )

        # Build the typed task input
        input_cls = _ENTITY_CONTRACTS.get(entity.name, (ExtractionTaskInput, None))[0]

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
        result_key = entity.result_key or f"{entity.name}_extracted"
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

            # Group by phase
            phases: dict[int, list[EntityDef]] = {}
            for entity in entities:
                phases.setdefault(entity.phase, []).append(entity)

            # Execute phase by phase
            results: dict[str, int] = {}
            for phase_num in sorted(phases):
                phase_results = await asyncio.gather(
                    *[self._fetch_entity(entity, input) for entity in phases[phase_num]]
                )
                for result_key, count in phase_results:
                    results[result_key] = count

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
            entity_counts = {k: v for k, v in results.items() if k in output_fields}

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
