import asyncio
import logging
import os
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from application_sdk.paas.readers.json import JSONChunkedObjectStoreReader
from application_sdk.paas.secretstore import SecretStore
from application_sdk.paas.writers.json import JSONChunkedObjectStoreWriter
from application_sdk.workflows.resources import TemporalConfig, TemporalResource
from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)
from application_sdk.workflows.sql.utils import prepare_filters
from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.utils.activity import auto_heartbeater
from application_sdk.workflows.workflow import WorkflowInterface

logger = logging.getLogger(__name__)


@workflow.defn
class SQLWorkflow(WorkflowInterface):
    fetch_database_sql = ""
    fetch_schema_sql = ""
    fetch_table_sql = ""
    fetch_column_sql = ""

    sql_resource: SQLResource | None = None
    transformer: TransformerInterface | None = None

    application_name: str = "sql-connector"
    batch_size: int = 100000
    max_transform_concurrency: int = 5

    # Note: the defaults are passed as temporal tries to initialize the workflow with no args
    def __init__(self):
        super().__init__()

    def set_sql_resource(self, sql_resource: SQLResource) -> "SQLWorkflow":
        self.sql_resource = sql_resource
        return self

    def set_transformer(self, transformer: TransformerInterface) -> "SQLWorkflow":
        self.transformer = transformer
        return self

    def set_application_name(self, application_name: str) -> "SQLWorkflow":
        self.application_name = application_name
        return self

    def set_batch_size(self, batch_size: int) -> "SQLWorkflow":
        self.batch_size = batch_size
        return self

    def set_max_transform_concurrency(
        self, max_transform_concurrency: int
    ) -> "SQLWorkflow":
        self.max_transform_concurrency = max_transform_concurrency
        return self

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
    ) -> "SQLWorkflow":
        super().set_temporal_resource(temporal_resource)
        return self

    def get_activities(self) -> List[Callable[..., Any]]:
        return [
            self.fetch_databases,
            self.fetch_schemas,
            self.fetch_tables,
            self.fetch_columns,
            self.transform_data,
            self.write_type_metadata,
        ] + super().get_activities()

    def store_credentials(self, credentials: Dict[str, Any]) -> str:
        return SecretStore.store_credentials(credentials)

    async def start(
        self, workflow_args: Dict[str, Any], workflow_class: Any | None = None
    ) -> Dict[str, Any]:
        """
        Run the workflow.

        :param workflow_args: The workflow arguments.
        :return: The workflow results.
        """
        workflow_args["credential_guid"] = self.store_credentials(
            workflow_args["credentials"]
        )
        del workflow_args["credentials"]

        workflow_class = workflow_class or self.__class__

        return await super().start(workflow_args, workflow_class)

    async def fetch_data(
        self, workflow_args: Dict[str, Any], query: str, typename: str
    ) -> int:
        """
        Fetch data from the database.

        :param workflow_args: The workflow arguments.
        :param query: The query to run.
        :param typename: The type of data to fetch.
        :return: The fetched data.
        :raises Exception: If the data cannot be fetched.
        """
        output_path = workflow_args["output_path"]

        raw_files_prefix = os.path.join(output_path, "raw", f"{typename}")
        raw_files_output_prefix = workflow_args["output_prefix"]

        try:
            async with (
                JSONChunkedObjectStoreWriter(
                    raw_files_prefix, raw_files_output_prefix
                ) as raw_writer,
            ):
                if self.sql_resource is None:
                    raise ValueError("SQL resource is not set")

                async for batch in self.sql_resource.run_query(query, self.batch_size):
                    # Write raw data
                    await raw_writer.write_list(batch)

                write_data = await raw_writer.write_metadata()
                return write_data["chunk_count"]

        except Exception as e:
            logger.error(f"Error fetching databases: {e}")
            raise e

    async def _transform_batch(
        self,
        results: List[Dict[str, Any]],
        typename: str,
        writer: JSONChunkedObjectStoreWriter,
    ) -> None:
        """
        Process a batch of results.

        :param results: The batch of results.
        :param typename: The type of data to fetch.
        :param writer: The writer to use.
        :raises Exception: If the results cannot be processed.
        """

        for row in results:
            try:
                transformed_metadata: Optional[Dict[str, Any]] = (
                    self.transformer.transform_metadata(typename, row)
                )
                if transformed_metadata is not None:
                    await writer.write(transformed_metadata)
                else:
                    activity.logger.warning(f"Skipped invalid {typename} data: {row}")
            except Exception as row_error:
                activity.logger.error(
                    f"Error processing row for {typename}: {row_error}"
                )

    @activity.defn
    @auto_heartbeater
    async def fetch_databases(self, workflow_args: Dict[str, Any]):
        """
        Fetch and process databases from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched databases.
        """
        chunk_count = await self.fetch_data(
            workflow_args, self.fetch_database_sql, "database"
        )
        return {"typename": "database", "chunk_count": chunk_count}

    @activity.defn
    @auto_heartbeater
    async def fetch_schemas(self, workflow_args: Dict[str, Any]):
        """
        Fetch and process schemas from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched schemas.
        """
        include_filter = workflow_args.get("metadata", {}).get("include-filter", "{}")
        exclude_filter = workflow_args.get("metadata", {}).get("exclude-filter", "{}")
        temp_table_regex = workflow_args.get("metadata", {}).get("temp-table-regex", "")
        normalized_include_regex, normalized_exclude_regex, _ = prepare_filters(
            include_filter,
            exclude_filter,
            temp_table_regex,
        )
        schema_sql_query = self.fetch_schema_sql.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
        )
        chunk_count = await self.fetch_data(workflow_args, schema_sql_query, "schema")
        return {"typename": "schema", "chunk_count": chunk_count}

    @activity.defn
    @auto_heartbeater
    async def fetch_tables(self, workflow_args: Dict[str, Any]):
        """
        Fetch and process tables from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched tables.
        """
        include_filter = workflow_args.get("metadata", {}).get("include-filter", "{}")
        exclude_filter = workflow_args.get("metadata", {}).get("exclude-filter", "{}")
        temp_table_regex = workflow_args.get("metadata", {}).get("temp-table-regex", "")
        normalized_include_regex, normalized_exclude_regex, exclude_table = (
            prepare_filters(
                include_filter,
                exclude_filter,
                temp_table_regex,
            )
        )
        table_sql_query = self.fetch_table_sql.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
            exclude_table=exclude_table,
        )
        chunk_count = await self.fetch_data(workflow_args, table_sql_query, "table")
        return {"typename": "table", "chunk_count": chunk_count}

    @activity.defn
    @auto_heartbeater
    async def fetch_columns(self, workflow_args: Dict[str, Any]):
        """
        Fetch and process columns from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched columns.
        """
        include_filter = workflow_args.get("metadata", {}).get("include-filter", "{}")
        exclude_filter = workflow_args.get("metadata", {}).get("exclude-filter", "{}")
        temp_table_regex = workflow_args.get("metadata", {}).get("temp-table-regex", "")
        normalized_include_regex, normalized_exclude_regex, exclude_table = (
            prepare_filters(
                include_filter,
                exclude_filter,
                temp_table_regex,
            )
        )
        column_sql_query = self.fetch_column_sql.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
            exclude_table=exclude_table,
        )

        chunk_count = await self.fetch_data(workflow_args, column_sql_query, "column")
        return {"typename": "column", "chunk_count": chunk_count}

    @activity.defn
    @auto_heartbeater
    async def write_type_metadata(self, workflow_args: Dict[str, Any]):
        chunk_count = workflow_args["chunk_count"]
        record_count = workflow_args["record_count"]
        output_path = workflow_args["output_path"]
        output_prefix = workflow_args["output_prefix"]
        typename = workflow_args["typename"]

        transform_files_prefix = os.path.join(output_path, "transformed", f"{typename}")
        transform_files_output_prefix = output_prefix

        async with (
            JSONChunkedObjectStoreWriter(
                transform_files_prefix,
                transform_files_output_prefix,
                start_file_number=chunk_count,
            ) as transformed_writer,
        ):
            await transformed_writer.write_metadata(total_record_count=record_count)

    @activity.defn
    @auto_heartbeater
    async def transform_data(self, workflow_args: Dict[str, Any]) -> int:
        batch = workflow_args["batch"]
        chunk_start = workflow_args["chunk_start"]
        typename = workflow_args["typename"]
        output_path = workflow_args["output_path"]
        output_prefix = workflow_args["output_prefix"]

        transform_files_prefix = os.path.join(output_path, "transformed", f"{typename}")
        transform_files_output_prefix = output_prefix

        raw_files_prefix = os.path.join(output_path, "raw")
        raw_files_output_prefix = workflow_args["output_prefix"]

        async with (
            JSONChunkedObjectStoreReader(
                raw_files_prefix, raw_files_output_prefix, typename
            ) as raw_reader,
            JSONChunkedObjectStoreWriter(
                transform_files_prefix,
                transform_files_output_prefix,
                start_file_number=chunk_start,
            ) as transformed_writer,
        ):
            raw_data: List[Any] = []
            for chunk in batch:
                raw_data += await raw_reader.read_chunk(chunk)
                await self._transform_batch(raw_data, typename, transformed_writer)

            write_data = await transformed_writer.write_metadata()
            return write_data["total_record_count"]

    def get_transform_batches(self, chunk_count: int, typename: str):
        # concurrency logic
        concurrency_level = min(
            self.max_transform_concurrency,
            chunk_count,
        )

        batches: List[List[str]] = []
        chunk_start_numbers: List[int] = []
        start = 0
        for i in range(concurrency_level):
            current_batch_start = start
            chunk_start_numbers.append(current_batch_start)
            current_batch_count = int(chunk_count / concurrency_level)
            if i < chunk_count % concurrency_level and chunk_count > concurrency_level:
                current_batch_count += 1

            batches.append(
                [
                    f"{typename}-{i}.json"
                    for i in range(
                        current_batch_start + 1,
                        current_batch_start + current_batch_count + 1,
                    )
                ]
            )
            start += current_batch_count

        return batches, chunk_start_numbers

    async def fetch_and_transform(self, fetch_fn, workflow_args, retry_policy):
        raw_stat = await workflow.execute_activity(
            fetch_fn,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        transform_activities: List[Any] = []

        typename: str = raw_stat["typename"]
        chunk_count: int = raw_stat["chunk_count"]

        batches, chunk_starts = self.get_transform_batches(chunk_count, typename)

        for i in range(len(batches)):
            transform_activities.append(
                workflow.execute_activity(
                    self.transform_data,
                    {
                        "typename": typename,
                        "batch": batches[i],
                        "chunk_start": chunk_starts[i],
                        **workflow_args,
                    },
                    retry_policy=retry_policy,
                    start_to_close_timeout=timedelta(seconds=1000),
                )
            )

        record_counts = await asyncio.gather(*transform_activities)
        total_record_count = sum(record_counts)

        await workflow.execute_activity(
            self.write_type_metadata,
            {
                "record_count": total_record_count,
                "chunk_count": len(batches),
                "typename": typename,
                **workflow_args,
            },
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

    @workflow.run
    async def run(self, workflow_args: Dict[str, Any]):
        """
        Run the workflow.

        :param workflow_args: The workflow arguments.
        """
        if not self.sql_resource:
            credentials = SecretStore.extract_credentials(
                workflow_args["credential_guid"]
            )
            self.sql_resource = SQLResource(
                SQLResourceConfig(
                    credentials=credentials,
                )
            )

        if not self.temporal_resource:
            self.temporal_resource = TemporalResource(
                TemporalConfig(application_name=self.application_name)
            )

        workflow_id = workflow_args["workflow_id"]
        workflow.logger.info(f"Starting extraction workflow for {workflow_id}")
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

        workflow_run_id = workflow.info().run_id
        output_prefix = workflow_args["output_prefix"]
        output_path = f"{output_prefix}/{workflow_id}/{workflow_run_id}"
        workflow_args["output_path"] = output_path

        fetch_and_transforms = [
            self.fetch_and_transform(self.fetch_databases, workflow_args, retry_policy),
            self.fetch_and_transform(self.fetch_schemas, workflow_args, retry_policy),
            self.fetch_and_transform(self.fetch_tables, workflow_args, retry_policy),
            self.fetch_and_transform(self.fetch_columns, workflow_args, retry_policy),
        ]

        await asyncio.gather(*fetch_and_transforms)

        workflow.logger.info(f"Extraction workflow completed for {workflow_id}")
