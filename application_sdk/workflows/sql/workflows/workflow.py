import asyncio
import logging
import os
from datetime import timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional

import pandas as pd
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from application_sdk import activity_pd
from application_sdk.inputs.secretstore import SecretStore
from application_sdk.outputs.json import JSONChunkedObjectStoreWriter, JsonOutput
from application_sdk.paas.readers.json import JSONChunkedObjectStoreReader
from application_sdk.workflows.resources.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)
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
        if self.sql_resource is None:
            raise ValueError("SQL resource is not set")

        self.sql_resource.set_credentials(workflow_args["credentials"])
        await self.sql_resource.load()

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
        if self.sql_resource is None:
            raise ValueError("SQL resource is not set")

        output_path = workflow_args["output_path"]

        raw_files_prefix = os.path.join(output_path, "raw", f"{typename}")
        raw_files_output_prefix = workflow_args["output_prefix"]

        try:
            async with (
                JSONChunkedObjectStoreWriter(
                    raw_files_prefix, raw_files_output_prefix
                ) as raw_writer,
            ):
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
        workflow_id: str,
        workflow_run_id: str,
    ) -> int:
        """
        Process a batch of results.

        :param results: The batch of results.
        :param typename: The type of data to fetch.
        :param writer: The writer to use.
        :raises Exception: If the results cannot be processed.
        """
        if self.transformer is None:
            raise ValueError("Transformer is not set")

        for row in results:
            try:
                if not self.transformer:
                    raise ValueError("Transformer is not set")

                transformed_metadata: Optional[Dict[str, Any]] = (
                    self.transformer.transform_metadata(
                        typename,
                        row,
                        workflow_id=workflow_id,
                        workflow_run_id=workflow_run_id,
                    )
                )
                if transformed_metadata is not None:
                    await writer.write(transformed_metadata)
                else:
                    activity.logger.warning(f"Skipped invalid {typename} data: {row}")
            except Exception as row_error:
                activity.logger.error(
                    f"Error processing row for {typename}: {row_error}"
                )
        return writer.total_record_count

    @staticmethod
    def prepare_query(query: str, workflow_args: Dict[str, Any]) -> str:
        """
        Method to prepare the query with the include and exclude filters
        """
        try:
            include_filter = workflow_args.get(
                "metadata", workflow_args.get("form_data", {})
            ).get("include_filter", "{}")
            exclude_filter = workflow_args.get(
                "metadata", workflow_args.get("form_data", {})
            ).get("exclude_filter", "{}")
            temp_table_regex = workflow_args.get(
                "metadata", workflow_args.get("form_data", {})
            ).get("temp_table_regex", "")
            normalized_include_regex, normalized_exclude_regex, exclude_table = (
                prepare_filters(
                    include_filter,
                    exclude_filter,
                    temp_table_regex,
                )
            )
            exclude_empty_tables = workflow_args.get("metadata", {}).get(
                "exclude_empty_tables", False
            )
            exclude_views = workflow_args.get("metadata", {}).get(
                "exclude_views", False
            )

            return query.format(
                normalized_include_regex=normalized_include_regex,
                normalized_exclude_regex=normalized_exclude_regex,
                exclude_table=exclude_table,
                exclude_empty_tables=exclude_empty_tables,
                exclude_views=exclude_views,
            )
        except Exception as e:
            logger.error(f"Error preparing query [{query}]:  {e}")

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            engine=self.sql_resource.engine,
            query=SQLWorkflow.prepare_query(
                query=self.fetch_database_sql, workflow_args=workflow_args
            ),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/database",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_databases(self, batch_input: pd.DataFrame, raw_output: JsonOutput):
        """
        Fetch and process databases from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched databases.
        """
        await raw_output.write_batched_df(batch_input)
        return {"batch_input": batch_input, "typename": "database"}

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            engine=self.sql_resource.engine,
            query=SQLWorkflow.prepare_query(
                query=self.fetch_schema_sql, workflow_args=workflow_args
            ),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/schema",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_schemas(self, batch_input: pd.DataFrame, raw_output: JsonOutput):
        """
        Fetch and process schemas from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched schemas.
        """
        await raw_output.write_batched_df(batch_input)
        return {"batch_input": batch_input, "typename": "schema"}

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            self.sql_resource.engine,
            query=SQLWorkflow.prepare_query(
                query=self.fetch_table_sql, workflow_args=workflow_args
            ),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/table",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_tables(self, batch_input: pd.DataFrame, raw_output: JsonOutput):
        """
        Fetch and process tables from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched tables.
        """
        await raw_output.write_batched_df(batch_input)
        return {"batch_input": batch_input, "typename": "table"}

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            self.sql_resource.engine,
            query=SQLWorkflow.prepare_query(
                query=self.fetch_column_sql, workflow_args=workflow_args
            ),
        ),
        raw_output=lambda self, workflow_args: JsonOutput(
            output_path=f"{workflow_args['output_path']}/raw/column",
            upload_file_prefix=workflow_args["output_prefix"],
        ),
    )
    async def fetch_columns(self, batch_input: pd.DataFrame, raw_output: JsonOutput):
        """
        Fetch and process columns from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched columns.
        """
        await raw_output.write_batched_df(batch_input)
        return {"batch_input": batch_input, "typename": "column"}

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

        workflow_id = workflow_args.get("workflow_id", None)
        workflow_run_id = workflow_args.get("workflow_run_id", None)

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
            total_transformed_data = 0
            for chunk in batch:
                raw_data += await raw_reader.read_chunk(chunk)
                total_transformed_data += await self._transform_batch(
                    raw_data,
                    typename,
                    transformed_writer,
                    workflow_id=workflow_id,
                    workflow_run_id=workflow_run_id,
                )

            return total_transformed_data

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
                    f"{typename}/{i}.json"
                    for i in range(
                        current_batch_start + 1,
                        current_batch_start + current_batch_count + 1,
                    )
                ]
            )
            start += current_batch_count

        return batches, chunk_start_numbers

    async def fetch_and_transform(
        self,
        fetch_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]],
        workflow_args: Dict[str, Any],
        retry_policy: RetryPolicy,
    ) -> None:
        raw_stat = await workflow.execute_activity(
            fetch_fn,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        transform_activities: List[Any] = []

        chunk_count = len(raw_stat)
        if chunk_count is None:
            raise ValueError("Invalid chunk_count")

        if chunk_count == 0:
            return

        typename = raw_stat[0].get("typename")
        if typename is None:
            raise ValueError("Invalid typename")

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
            self.sql_resource = SQLResource(SQLResourceConfig())

        credentials = SecretStore.extract_credentials(workflow_args["credential_guid"])
        self.sql_resource.set_credentials(credentials)

        if not self.temporal_resource:
            self.temporal_resource = TemporalResource(
                TemporalConfig(application_name=self.application_name)
            )

        workflow_id = workflow_args["workflow_id"]
        workflow_run_id = workflow.info().run_id
        workflow_args["workflow_run_id"] = workflow_run_id

        workflow.logger.info(f"Starting extraction workflow for {workflow_id}")
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

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
