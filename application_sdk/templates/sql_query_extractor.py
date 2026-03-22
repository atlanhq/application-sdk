"""SQL query extraction App — v3 implementation.

Replaces the v2 ``SQLQueryExtractionWorkflow`` + ``SQLQueryExtractionActivities``
split with a single typed ``App`` class.

Migration from v2::

    # v2
    from application_sdk.workflows.query_extraction.sql import SQLQueryExtractionWorkflow
    from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities

    # v3
    from application_sdk.templates import SqlQueryExtractor

Subclass to implement connector-specific logic::

    class MyQueryExtractor(SqlQueryExtractor):
        @task(timeout_seconds=3600)
        async def fetch_queries(self, input: QueryFetchInput) -> QueryFetchOutput:
            # connector-specific query fetching
            return QueryFetchOutput(queries_fetched=100)
"""

from __future__ import annotations

from loguru import logger

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.common.exc_utils import rewrap
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput,
    QueryBatchOutput,
    QueryExtractionInput,
    QueryExtractionOutput,
    QueryFetchInput,
    QueryFetchOutput,
)


class SqlQueryExtractor(App):
    """Base class for SQL query extraction apps.

    Subclass this and override the ``@task`` methods to implement
    connector-specific query extraction logic.

    The ``run()`` method orchestrates the full extraction:
    get_query_batches → fetch_queries (per batch) → aggregate output.
    """

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

    async def run(self, input: QueryExtractionInput) -> QueryExtractionOutput:  # type: ignore[override]
        """Orchestrate the full query extraction pipeline.

        Default orchestration:
        1. Get batch count
        2. Fetch each batch sequentially
        3. Return aggregated output

        Override to customize batch processing (e.g., parallel batches).
        """
        workflow_id = input.workflow_id
        logger.info("Starting SQL query extraction", workflow_id=workflow_id)

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

            logger.info(
                "Query extraction completed",
                workflow_id=workflow_id,
                total_batches=batch_result.total_batches,
                total_queries=total_queries,
            )

            return QueryExtractionOutput(
                workflow_id=workflow_id,
                success=True,
                total_batches=batch_result.total_batches,
                total_queries=total_queries,
            )

        except Exception as e:
            raise rewrap(
                e, f"SQL query extraction failed (workflow_id={workflow_id})"
            ) from e
