import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional, Sequence

from sqlalchemy import Connection, Engine, text
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.types import CallableType

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.paas.readers.json import JSONChunkedObjectStoreReader
from application_sdk.paas.secretstore import SecretStore
from application_sdk.paas.writers.json import JSONChunkedObjectStoreWriter
from application_sdk.workflows import WorkflowWorkerInterface
from application_sdk.workflows.sql.utils import prepare_filters
from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.utils.activity import auto_heartbeater

logger = logging.getLogger(__name__)

workflow.logger = AtlanLoggerAdapter(logging.getLogger(__name__))
activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class SQLWorkflowWorkerInterface(WorkflowWorkerInterface):
    """
    Base class for SQL workflow workers implementing the Template Method pattern.

    This class provides a default implementation for the workflow, with hooks
    for subclasses to customize specific behaviors.

    Attributes:
        DATABASE_SQL (str): SQL query to fetch database information.
        SCHEMA_SQL (str): SQL query to fetch schema information.
        TABLE_SQL (str): SQL query to fetch table information.
        COLUMN_SQL (str): SQL query to fetch column information.

    Usage:
        Subclass this class and override the SQL query attributes and any methods
        that need custom behavior. Then use the subclass to create a workflow builder
        and run the workflow.
    """

    DATABASE_SQL = ""
    SCHEMA_SQL = ""
    TABLE_SQL = ""
    COLUMN_SQL = ""

    # Note: the defaults are passed as temporal tries to initialize the workflow with no args
    def __init__(
        self,
        transformer: TransformerInterface,
        temporal_activities: Sequence[CallableType] = None,
        get_sql_engine: Callable[[Dict[str, Any]], Engine] = None,
        # Configuration
        application_name: str = "sql-connector",
        use_server_side_cursor: bool = True,
        batch_size: int = 10,
        max_transform_concurrency: int = 5,
        # TODO:
        chunk_size: int = 3000,
    ):
        """
        Initialize the SQL workflow worker.

        :param application_name: The name of the application (default: "sql-connector")
        :param get_sql_engine: A callable that returns an SQLAlchemy engine (default: None)
        :param use_server_side_cursor: Whether to use server-side cursor (default: True)
        :param temporal_activities: The temporal activities to run (default: [], parent class activities)
        :param transformer: The transformer to use (default: PhoenixTransformer)
        :param batch_size: The size of batches for running queries and transformation (default: 10)
        :param max_transform_concurrency: The maximum concurrency for transformation activities (default: 5)
        """
        self.get_sql_engine = get_sql_engine
        self.use_server_side_cursor = use_server_side_cursor
        self.transformer = transformer

        self.batch_size = batch_size
        self.max_transform_concurrency = max_transform_concurrency

        if not temporal_activities:
            # default activities
            self.TEMPORAL_ACTIVITIES = [
                self.fetch_databases,
                self.fetch_schemas,
                self.fetch_tables,
                self.fetch_columns,
                self.transform_data,
            ]
        else:
            self.TEMPORAL_ACTIVITIES = temporal_activities

        super().__init__(application_name)

    async def start_workflow(self, workflow_args: Any) -> Dict[str, Any]:
        """
        Run the workflow.

        :param workflow_args: The workflow arguments.
        :return: The workflow results.
        """
        credentials = workflow_args["credentials"]

        credential_guid = SecretStore.store_credentials(credentials)
        del workflow_args["credentials"]

        workflow_args["credential_guid"] = credential_guid
        return await super().start_workflow(workflow_args)

    async def run_query(self, connection: Connection, query: str, batch_size: int):
        """
        Run a query in a batch mode with client-side cursor.

        This method also supports server-side cursor via sqlalchemy execution options(yield_per=batch_size)
        If yield_per is not supported by the database, the method will fallback to client-side cursor.

        :param connection: The database connection.
        :param query: The query to run.
        :param batch_size: The batch size.
        :return: The query results.
        :raises Exception: If the query fails.
        """
        loop = asyncio.get_running_loop()

        if self.use_server_side_cursor:
            connection.execution_options(yield_per=batch_size)

        with ThreadPoolExecutor() as pool:
            try:
                cursor = await loop.run_in_executor(
                    pool, connection.execute, text(query)
                )
                column_names: List[str] = []

                while True:
                    rows = await loop.run_in_executor(
                        pool, cursor.fetchmany, batch_size
                    )
                    if not rows:
                        break

                    if not column_names:
                        column_names = rows[0]._fields

                    results = [dict(zip(column_names, row)) for row in rows]
                    yield results
            except Exception as e:
                logger.error(f"Error running query in batch: {e}")
                raise e

        activity.logger.info("Query execution completed")

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
        credentials = SecretStore.extract_credentials(workflow_args["credential_guid"])
        output_path = workflow_args["output_path"]

        raw_files_prefix = os.path.join(output_path, "raw", f"{typename}")
        raw_files_output_prefix = os.path.join(
            workflow_args["output_prefix"], "raw", f"{typename}"
        )

        try:
            engine = self.get_sql_engine(credentials)
            with engine.connect() as connection:
                async with (
                    JSONChunkedObjectStoreWriter(
                        raw_files_prefix, raw_files_output_prefix
                    ) as raw_writer,
                ):
                    async for batch in self.run_query(
                        connection, query, self.batch_size
                    ):
                        # Write raw data
                        await raw_writer.write_list(batch)

                    return await raw_writer.close()

        except Exception as e:
            logger.error(f"Error fetching databases: {e}")
            raise e

        return -1

    async def fetch_and_process_data(
        self, workflow_args: Dict[str, Any], query: str, typename: str
    ):
        """
        Fetch and process data from the database.

        :param workflow_args: The workflow arguments.
        :param query: The query to run.
        :param typename: The type of data to fetch.
        :return: The fetched data.
        :raises Exception: If the data cannot be fetched.
        """
        output_path = workflow_args["output_path"]

        transform_files_prefix = os.path.join(output_path, "transformed", f"{typename}")
        transform_files_output_prefix = os.path.join(
            workflow_args["output_prefix"], "transformed", f"{typename}"
        )

        # Fetches data and stores it in raw files
        await self.fetch_data(workflow_args, query, typename)

        try:
            async with (
                JSONChunkedObjectStoreWriter(
                    transform_files_prefix, transform_files_output_prefix
                ) as transformed_writer,
            ):
                # Transforms data
                await self._transform_batch(batch, typename, transformed_writer)
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
            workflow_args, self.DATABASE_SQL, "database"
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
        schema_sql_query = self.SCHEMA_SQL.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
        )
        return await self.fetch_and_process_data(
            workflow_args, schema_sql_query, "schema"
        )

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
        table_sql_query = self.TABLE_SQL.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
            exclude_table=exclude_table,
        )
        return await self.fetch_and_process_data(
            workflow_args, table_sql_query, "table"
        )

    @activity.defn
    @auto_heartbeater
    async def transform_data(self, workflow_args: Dict[str, Any]):
        batch = workflow_args["batch"]
        typename = workflow_args["typename"]
        output_path = workflow_args["output_path"]
        output_prefix = workflow_args["output_prefix"]

        transform_files_prefix = os.path.join(output_path, "transformed", f"{typename}")
        transform_files_output_prefix = os.path.join(
            output_prefix, "transformed", f"{typename}"
        )

        raw_files_prefix = os.path.join(output_path, "raw", f"{typename}")
        raw_files_output_prefix = os.path.join(
            workflow_args["output_prefix"], "raw", f"{typename}"
        )

        async with (
            JSONChunkedObjectStoreReader(
                raw_files_prefix, raw_files_output_prefix
            ) as raw_reader,
            JSONChunkedObjectStoreWriter(
                transform_files_prefix, transform_files_output_prefix
            ) as transformed_writer,
        ):
            data = []
            for batch in batches:
                data += await raw_reader.read_chunk(typename, batch[0])

            await self._transform_batch(data, typename, transformed_writer)

            transformed_writer.write_list(data)

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
        column_sql_query = self.COLUMN_SQL.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
            exclude_table=exclude_table,
        )

        return await self.fetch_data(workflow_args, column_sql_query, "column")

    @activity.defn
    @auto_heartbeater
    async def fetch_sql(self, sql: str):
        """
        Fetch data from the database using a custom SQL query.

        :param sql: The SQL query to run.
        :return: The fetched data.
        """
        pass

    @workflow.run
    async def run(self, workflow_args: Dict[str, Any]):
        """
        Run the workflow.

        :param workflow_args: The workflow arguments.
        """
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

        # run activities in parallel
        activities: List[Coroutine[Any, Any, Any]] = [
            workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
                self.fetch_databases,
                workflow_args,
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            ),
            # workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            #     self.fetch_schemas,
            #     workflow_args,
            #     retry_policy=retry_policy,
            #     start_to_close_timeout=timedelta(seconds=1000),
            # ),
            # workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            #     self.fetch_tables,
            #     workflow_args,
            #     retry_policy=retry_policy,
            #     start_to_close_timeout=timedelta(seconds=1000),
            # ),
            # workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            #     self.fetch_columns,
            #     workflow_args,
            #     retry_policy=retry_policy,
            #     heartbeat_timeout=timedelta(seconds=120),
            #     start_to_close_timeout=timedelta(seconds=1000),
            # ),
        ]

        raw_stat = await asyncio.gather(*activities)
        transform_activities: List[Any] = []

        for stat in raw_stat:
            typename = stat["typename"]
            chunk_count = stat["chunk_count"]

            # concurrency logic
            concurrency_level = min(
                self.max_transform_concurrency,
                chunk_count,
            )

            batches: List[List[int]] = []
            start = 0
            for i in range(concurrency_level):
                current_batch_start = start
                current_batch_count = int(chunk_count / concurrency_level)
                if i < chunk_count % concurrency_level:
                    current_batch_count += 1
                batches.append(
                    [
                        current_batch_start,
                        current_batch_start + current_batch_count,
                    ]
                )
                start += current_batch_count

            for batch in batches:
                transform_activities.append(
                    workflow.execute_activity(
                        self.transform_data,
                        {"typename": typename, "batch": batch, **workflow_args},
                        retry_policy=retry_policy,
                        start_to_close_timeout=timedelta(seconds=1000),
                    )
                )

        await asyncio.gather(*transform_activities)

        workflow.logger.info(f"Extraction workflow completed for {workflow_id}")
