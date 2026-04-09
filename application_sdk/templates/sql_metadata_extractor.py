"""SQL metadata extraction App — v3 implementation.

Replaces the v2 ``BaseSQLMetadataExtractionWorkflow`` +
``BaseSQLMetadataExtractionActivities`` split with a single typed ``App`` class.

Migration from v2::

    # v2: separate workflow + activities, all Dict[str, Any]
    from application_sdk.workflows.metadata_extraction.sql import (
        BaseSQLMetadataExtractionWorkflow,
    )
    from application_sdk.activities.metadata_extraction.sql import (
        BaseSQLMetadataExtractionActivities,
    )

    # v3: single App class with typed contracts
    from application_sdk.templates import SqlMetadataExtractor

Subclass ``SqlMetadataExtractor`` to implement connector-specific logic::

    from application_sdk.templates import SqlMetadataExtractor
    from application_sdk.templates.contracts.sql_metadata import (
        ExtractionInput, ExtractionOutput, FetchDatabasesInput, FetchDatabasesOutput,
    )
    from application_sdk.app import task

    class MyConnectorExtractor(SqlMetadataExtractor):
        @task(timeout_seconds=1800)
        async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
            # connector-specific implementation
            return FetchDatabasesOutput(chunk_count=1, total_record_count=10)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from application_sdk.app.base import App
from application_sdk.app.task import task
from application_sdk.common.exc_utils import rewrap
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
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

logger = get_logger(__name__)

if TYPE_CHECKING:
    pass


class SqlMetadataExtractor(App):
    """Base class for SQL metadata extraction apps.

    Subclass this and override the ``@task`` methods to implement
    connector-specific extraction logic.

    The ``run()`` method orchestrates the full extraction: preflight →
    fetch (databases, schemas, tables, columns) → transform → upload.
    Override ``run()`` to change the orchestration.

    All task timeouts default to 30 minutes. Override via::

        @task(timeout_seconds=3600)
        async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
            ...
    """

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        """Fetch databases from the source system.

        Override this method in your connector subclass.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_databases(). "
            "See application_sdk.templates.sql_metadata_extractor for examples."
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
        """Fetch stored procedures from the source system.

        This task is optional — connectors that do not support stored procedures
        should return ``FetchProceduresOutput()`` with zero counts rather than
        raising an error.

        This task is NOT called from the base ``run()`` method. Connectors that
        need it should call it from their own ``run()`` override.

        Override this method in your connector subclass if procedure extraction
        is required.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement fetch_procedures(), "
            "or return FetchProceduresOutput() with zero counts for connectors "
            "that do not support stored procedures."
        )

    @task(timeout_seconds=1800)
    async def transform_data(self, input: TransformInput) -> TransformOutput:
        """Transform raw extracted data into the target format."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement transform_data()."
        )

    async def run(self, input: ExtractionInput) -> ExtractionOutput:  # type: ignore[override]
        """Orchestrate the full metadata extraction pipeline.

        Default orchestration:
        1. Fetch all metadata types in parallel (databases, schemas, tables, columns)
        2. Transform data
        3. Return aggregated output

        Override to customize the orchestration order or add additional steps.
        """
        workflow_id = input.workflow_id
        logger.info("Starting SQL metadata extraction: %s", workflow_id)

        try:
            # Prefer credential_ref; fall back to legacy credential_guid
            cred_ref = input.credential_ref
            if cred_ref is None and input.credential_guid:
                from application_sdk.credentials import legacy_credential_ref

                cred_ref = legacy_credential_ref(input.credential_guid)

            # Fetch all metadata types in parallel
            (
                db_result,
                schema_result,
                table_result,
                column_result,
            ) = await asyncio.gather(
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
                self.fetch_tables(
                    FetchTablesInput(
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
                self.fetch_columns(
                    FetchColumnsInput(
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

            logger.info(
                "Metadata extraction completed",
                workflow_id=workflow_id,
                databases=db_result.total_record_count,
                schemas=schema_result.total_record_count,
                tables=table_result.total_record_count,
                columns=column_result.total_record_count,
            )

            return ExtractionOutput(
                workflow_id=workflow_id,
                success=True,
                databases_extracted=db_result.total_record_count,
                schemas_extracted=schema_result.total_record_count,
                tables_extracted=table_result.total_record_count,
                columns_extracted=column_result.total_record_count,
            )

        except Exception as e:
            raise rewrap(
                e, f"SQL metadata extraction failed (workflow_id={workflow_id})"
            ) from e
