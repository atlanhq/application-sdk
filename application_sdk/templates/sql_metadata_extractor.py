"""SQL metadata extraction App.

Subclass ``SqlMetadataExtractor`` to implement connector-specific logic::

    from application_sdk.templates import SqlMetadataExtractor
    from application_sdk.templates.contracts.sql_metadata import (
        ExtractionInput, ExtractionOutput, FetchDatabasesInput, FetchDatabasesOutput,
    )
    from application_sdk.app import task

    class MyConnectorExtractor(SqlMetadataExtractor):
        sql_client_class = MySQLClient

        fetch_database_sql = "SELECT db_name FROM databases"

        @task(timeout_seconds=1800)
        async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
            return await super().fetch_databases(input)

Alternatively, override the method entirely without calling super():

    class MyConnectorExtractor(SqlMetadataExtractor):
        @task(timeout_seconds=1800)
        async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
            return FetchDatabasesOutput(chunk_count=1, total_record_count=10)
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from application_sdk.app.task import task
from application_sdk.common.exc_utils import rewrap
from application_sdk.credentials import CredentialResolver, legacy_credential_ref
from application_sdk.infrastructure.context import get_infrastructure
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

if TYPE_CHECKING:
    from application_sdk.clients.sql import BaseSQLClient
    from application_sdk.credentials.ref import CredentialRef

logger = get_logger(__name__)

_ET = TypeVar("_ET", bound=ExtractionTaskInput)


def _task_input(
    input_cls: type[_ET],
    src: ExtractionInput,
    *,
    cred_ref: "CredentialRef | None",
) -> _ET:
    """Build a typed task input from the top-level extraction input."""
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
        source_tag_prefix=src.source_tag_prefix,
    )


class SqlMetadataExtractor(BaseMetadataExtractor):
    """Base class for SQL metadata extraction apps.

    Inherits ``upload_to_atlan`` from ``BaseMetadataExtractor``.

    The ``run()`` method orchestrates the full extraction:
    fetch (databases, schemas, tables, columns) → transform → upload.
    Override ``run()`` to change the orchestration.

    All task timeouts default to 30 minutes. Override via::

        @task(timeout_seconds=3600)
        async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
            ...

    SQL-string pattern: set ``sql_client_class`` and the ``fetch_*_sql``
    class attributes on your subclass, then call ``super()`` from each
    ``@task`` override to use the default SQL execution provided here.
    """

    # Prevent auto-registration: SqlMetadataExtractor is a base template, not a
    # concrete app. Concrete subclasses (e.g. CloudSqlApp) register themselves.
    _app_registered: ClassVar[bool] = True

    # Set this to a BaseSQLClient subclass to enable default SQL execution.
    sql_client_class: ClassVar[type[BaseSQLClient] | None] = None

    # SQL templates — set in subclasses to use default execution via super().
    fetch_database_sql: ClassVar[str] = ""
    fetch_schema_sql: ClassVar[str] = ""
    fetch_table_sql: ClassVar[str] = ""
    fetch_column_sql: ClassVar[str] = ""

    # SQL fragments substituted into fetch_table_sql / fetch_column_sql when
    # temp_table_regex is set on the extraction input.
    extract_temp_table_regex_table_sql: ClassVar[str] = ""
    extract_temp_table_regex_column_sql: ClassVar[str] = ""

    # ------------------------------------------------------------------
    # Credential / client helpers (not @task — run in activity context)
    # ------------------------------------------------------------------

    async def _get_credentials(self, input: ExtractionTaskInput) -> dict[str, Any]:
        """Resolve credentials from the task input.

        Checks the Dapr state store first (local dev / combined mode), then
        falls back to the full CredentialResolver for production.
        """
        cred_guid = input.credential_guid
        is_local_dev = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )

        infra = get_infrastructure()

        if cred_guid and is_local_dev and infra and infra.state_store:
            data = await infra.state_store.load(f"cred:{cred_guid}")
            if data is not None:
                return data

        # Production path: use CredentialResolver

        ref = input.credential_ref or (
            legacy_credential_ref(cred_guid) if cred_guid else None
        )
        if ref is None:
            raise ValueError("No credential reference or GUID available in task input")

        secret_store = infra.secret_store if infra else None
        if secret_store is None:
            raise ValueError("No secret store available for credential resolution")

        resolver = CredentialResolver(secret_store)
        return await resolver.resolve_raw(ref)

    async def _load_sql_client(self, input: ExtractionTaskInput) -> BaseSQLClient:
        """Create and load a SQL client using resolved credentials.

        Raises:
            NotImplementedError: If ``sql_client_class`` is not set.
        """
        if self.sql_client_class is None:
            raise NotImplementedError(
                f"{type(self).__name__} must set sql_client_class to use "
                "default SQL execution from super()."
            )
        credentials = await self._get_credentials(input)
        client = self.sql_client_class()
        await client.load(credentials)
        return client

    def _prepare_sql(
        self, sql: str, input: ExtractionTaskInput, *, column_mode: bool = False
    ) -> str:
        """Substitute filter placeholders in a SQL template.

        Replaces ``{normalized_exclude_regex}``, ``{normalized_include_regex}``,
        and ``{temp_table_regex_sql}`` with values derived from *input*.
        Uses str.replace() to avoid conflicts with any other curly braces.
        """
        exclude_regex = input.exclude_filter or "^$"
        include_regex = input.include_filter or ".*"

        temp_table_sql = ""
        if input.temp_table_regex:
            fragment = (
                self.extract_temp_table_regex_column_sql
                if column_mode
                else self.extract_temp_table_regex_table_sql
            )
            if fragment:
                temp_table_sql = fragment.replace(
                    "{exclude_table_regex}", input.temp_table_regex
                )

        return (
            sql.replace("{normalized_exclude_regex}", exclude_regex)
            .replace("{normalized_include_regex}", include_regex)
            .replace("{temp_table_regex_sql}", temp_table_sql)
        )

    # ------------------------------------------------------------------
    # Fetch tasks — default implementations using SQL class attributes
    # ------------------------------------------------------------------

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        """Fetch databases from the source system.

        Default implementation executes ``self.fetch_database_sql`` via
        ``self.sql_client_class``.  Override in your subclass — or set those
        two class attributes and call ``super()`` — to use this default.
        """
        if not self.fetch_database_sql:
            raise NotImplementedError(
                f"{type(self).__name__} must implement fetch_databases() "
                "or set fetch_database_sql. "
                "See application_sdk.templates.sql_metadata_extractor for examples."
            )
        client = await self._load_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_database_sql.strip(), input)
            rows: list[dict[str, Any]] = []
            async for batch in client.run_query(sql):
                rows.extend(batch)
            databases = [
                str(row.get("database_name", ""))
                for row in rows
                if row.get("database_name")
            ]
            return FetchDatabasesOutput(
                databases=databases,
                chunk_count=1,
                total_record_count=len(databases),
            )
        finally:
            await client.close()

    @task(timeout_seconds=1800)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        """Fetch schemas from the source system.

        Default implementation executes ``self.fetch_schema_sql``.
        """
        if not self.fetch_schema_sql:
            raise NotImplementedError(
                f"{type(self).__name__} must implement fetch_schemas() "
                "or set fetch_schema_sql."
            )
        client = await self._load_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_schema_sql.strip(), input)
            rows: list[dict[str, Any]] = []
            async for batch in client.run_query(sql):
                rows.extend(batch)
            schemas = [
                str(row.get("schema_name", ""))
                for row in rows
                if row.get("schema_name")
            ]
            return FetchSchemasOutput(
                schemas=schemas,
                chunk_count=1,
                total_record_count=len(schemas),
            )
        finally:
            await client.close()

    @task(timeout_seconds=1800)
    async def fetch_tables(self, input: FetchTablesInput) -> FetchTablesOutput:
        """Fetch tables from the source system.

        Default implementation executes ``self.fetch_table_sql``.
        """
        if not self.fetch_table_sql:
            raise NotImplementedError(
                f"{type(self).__name__} must implement fetch_tables() "
                "or set fetch_table_sql."
            )
        client = await self._load_sql_client(input)
        try:
            sql = self._prepare_sql(self.fetch_table_sql.strip(), input)
            rows: list[dict[str, Any]] = []
            async for batch in client.run_query(sql):
                rows.extend(batch)
            tables = [
                str(row.get("table_name", "")) for row in rows if row.get("table_name")
            ]
            return FetchTablesOutput(
                tables=tables,
                chunk_count=1,
                total_record_count=len(tables),
            )
        finally:
            await client.close()

    @task(timeout_seconds=1800)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        """Fetch columns from the source system.

        Default implementation executes ``self.fetch_column_sql``.
        """
        if not self.fetch_column_sql:
            raise NotImplementedError(
                f"{type(self).__name__} must implement fetch_columns() "
                "or set fetch_column_sql."
            )
        client = await self._load_sql_client(input)
        try:
            sql = self._prepare_sql(
                self.fetch_column_sql.strip(), input, column_mode=True
            )
            total = 0
            async for batch in client.run_query(sql):
                total += len(batch)
            return FetchColumnsOutput(chunk_count=1, total_record_count=total)
        finally:
            await client.close()

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
            # v2-compat: remove credential_guid fallback when all connectors use credential_ref.
            # Prefer credential_ref; fall back to legacy credential_guid
            cred_ref = input.credential_ref
            if cred_ref is None and input.credential_guid:
                cred_ref = legacy_credential_ref(input.credential_guid)

            # Fetch all metadata types in parallel
            (
                db_result,
                schema_result,
                table_result,
                column_result,
            ) = await asyncio.gather(
                self.fetch_databases(
                    _task_input(FetchDatabasesInput, input, cred_ref=cred_ref)
                ),
                self.fetch_schemas(
                    _task_input(FetchSchemasInput, input, cred_ref=cred_ref)
                ),
                self.fetch_tables(
                    _task_input(FetchTablesInput, input, cred_ref=cred_ref)
                ),
                self.fetch_columns(
                    _task_input(FetchColumnsInput, input, cred_ref=cred_ref)
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

            # Upload extracted data to Atlan
            if input.output_path:
                upload_result = await self.upload_to_atlan(
                    UploadInput(output_path=input.output_path)
                )
                records_uploaded = upload_result.migrated_files
            else:
                records_uploaded = 0

            return ExtractionOutput(
                workflow_id=workflow_id,
                success=True,
                databases_extracted=db_result.total_record_count,
                schemas_extracted=schema_result.total_record_count,
                tables_extracted=table_result.total_record_count,
                columns_extracted=column_result.total_record_count,
                records_uploaded=records_uploaded,
            )

        except Exception as e:
            raise rewrap(
                e, f"SQL metadata extraction failed (workflow_id={workflow_id})"
            ) from e
