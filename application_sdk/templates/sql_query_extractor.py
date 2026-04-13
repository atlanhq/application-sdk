"""SQL query extraction App — v3 implementation.

Replaces the v2 ``SQLQueryExtractionWorkflow`` + ``SQLQueryExtractionActivities``
split with a single typed ``App`` class.

Subclass to implement connector-specific logic::

    from application_sdk.templates import SqlQueryExtractor
    from application_sdk.templates.entity import EntityDef

    class MyQueryExtractor(SqlQueryExtractor):
        entities = [
            EntityDef(name="queries", phase=1),
        ]

        @task(timeout_seconds=600)
        async def get_query_batches(self, input): ...

        @task(timeout_seconds=3600)
        async def fetch_queries(self, input): ...
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from application_sdk.app.task import task
from application_sdk.common.exc_utils import rewrap
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates.base_metadata_extractor import BaseMetadataExtractor
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput,
    QueryBatchOutput,
    QueryExtractionInput,
    QueryExtractionOutput,
    QueryFetchInput,
    QueryFetchOutput,
)
from application_sdk.templates.entity import EntityDef

logger = get_logger(__name__)

# Default entity definitions for query extraction.
_DEFAULT_QUERY_ENTITIES = [
    EntityDef(name="queries", phase=1),
]


class SqlQueryExtractor(BaseMetadataExtractor):
    """Base class for SQL query extraction apps.

    Inherits ``upload_to_atlan`` from ``BaseMetadataExtractor``.

    **Entity-driven orchestration:**

    Set the ``entities`` class variable to declare what to extract.
    The ``run()`` method automatically orchestrates all registered
    entities by phase — no need to override ``run()``.

    For the ``queries`` entity, orchestration calls
    ``get_query_batches()`` then loops ``fetch_queries()`` per batch.
    Custom entities dispatch to ``fetch_{name}()`` by convention.

    **Default behavior:**

    If ``entities`` is not overridden (empty list), the extractor
    falls back to a single ``queries`` entity.
    """

    entities: ClassVar[list[EntityDef]] = []
    """Override with a list of ``EntityDef`` to declare entities.
    Empty = use default (queries only)."""

    # ------------------------------------------------------------------
    # Task methods — override in subclass
    # ------------------------------------------------------------------

    @task(timeout_seconds=600)
    async def get_query_batches(self, input: QueryBatchInput) -> QueryBatchOutput:
        """Determine how many batches to process and their size.

        Override this method in your connector subclass.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement get_query_batches()."
        )

    @task(timeout_seconds=3600)
    async def fetch_queries(self, input: QueryFetchInput) -> QueryFetchOutput:
        """Fetch one batch of queries from the source system.

        Override this method in your connector subclass.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_queries()."
        )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def _get_entities(self) -> list[EntityDef]:
        """Return the effective entity list.

        If the subclass sets ``entities``, use that.
        Otherwise fall back to the default (queries only).
        """
        entities = self.entities if self.entities else _DEFAULT_QUERY_ENTITIES
        return [e for e in entities if e.enabled]

    async def _fetch_entity(
        self,
        entity: EntityDef,
        base_input: QueryExtractionInput,
        workflow_args: dict,
    ) -> tuple[str, int]:
        """Dispatch to the correct fetch method for an entity.

        For the ``queries`` entity, runs the batch loop
        (get_query_batches → fetch_queries per batch).
        For other entities, dispatches to ``fetch_{name}()``.

        Returns:
            Tuple of (result_key, count).
        """
        if entity.name == "queries":
            return await self._fetch_queries_batched(workflow_args)

        # Standard dispatch for custom entities
        method_name = f"fetch_{entity.name}"
        method = getattr(self, method_name, None)
        if method is None:
            raise NotImplementedError(
                f"{type(self).__name__} has no fetch_{entity.name}() method "
                f"for entity '{entity.name}'."
            )

        result = await method(base_input)
        result_key = entity.result_key or f"{entity.name}_extracted"
        count = getattr(result, "total_record_count", 0) or getattr(
            result, "queries_fetched", 0
        )
        return (result_key, count)

    async def _fetch_queries_batched(
        self,
        workflow_args: dict,
    ) -> tuple[str, int]:
        """Run the batch loop for query extraction.

        Calls ``get_query_batches()`` to determine batch count,
        then ``fetch_queries()`` for each batch sequentially.

        Returns:
            Tuple of ("total_queries", total_queries_fetched).
        """
        batch_result = await self.get_query_batches(
            QueryBatchInput(workflow_args=workflow_args)
        )

        total_queries = 0
        for batch_num in range(batch_result.total_batches):
            fetch_result = await self.fetch_queries(
                QueryFetchInput(
                    workflow_args=workflow_args,
                    batch_number=batch_num,
                    batch_size=batch_result.batch_size,
                )
            )
            total_queries += fetch_result.queries_fetched

        return ("total_queries", total_queries)

    async def run(self, input: QueryExtractionInput) -> QueryExtractionOutput:  # type: ignore[override]
        """Orchestrate the full query extraction pipeline.

        Executes entities grouped by phase.  All entities in the same
        phase run concurrently.  Phase N+1 starts only after phase N
        completes.
        """
        workflow_id = input.workflow_id
        logger.info("Starting SQL query extraction: %s", workflow_id)

        try:
            # Prefer credential_ref; fall back to legacy credential_guid
            cred_ref = input.credential_ref
            if cred_ref is None and input.credential_guid:
                from application_sdk.credentials import legacy_credential_ref

                cred_ref = legacy_credential_ref(input.credential_guid)

            workflow_args = {
                "workflow_id": workflow_id,
                "connection": input.connection,
                "credential_guid": input.credential_guid,
                "credential_ref": cred_ref,
                "output_prefix": input.output_prefix,
                "output_path": input.output_path,
                "lookback_days": input.lookback_days,
                "batch_size": input.batch_size,
            }

            entities = self._get_entities()

            # Group by phase
            phases: dict[int, list[EntityDef]] = {}
            for entity in entities:
                phases.setdefault(entity.phase, []).append(entity)

            # Execute phase by phase
            results: dict[str, int] = {}
            for phase_num in sorted(phases):
                phase_results = await asyncio.gather(
                    *[
                        self._fetch_entity(entity, input, workflow_args)
                        for entity in phases[phase_num]
                    ]
                )
                for result_key, count in phase_results:
                    results[result_key] = count

            logger.info(
                "Query extraction completed",
                workflow_id=workflow_id,
                results=results,
            )

            # Build output dynamically — any result key matching a
            # QueryExtractionOutput field gets populated automatically.
            output_fields = QueryExtractionOutput.model_fields
            entity_counts = {k: v for k, v in results.items() if k in output_fields}

            return QueryExtractionOutput(
                workflow_id=workflow_id,
                success=True,
                **entity_counts,
            )

        except Exception as e:
            raise rewrap(
                e, f"SQL query extraction failed (workflow_id={workflow_id})"
            ) from e
