
import asyncio
from datetime import timedelta
import shutil
import aiofiles
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import uuid
from typing import Any, Coroutine, Dict, List, Callable, Sequence

from sqlalchemy import Connection, Engine, text

from temporalio.common import RetryPolicy
from temporalio import activity, workflow
from temporalio.types import CallableType

from application_sdk.workflows.transformers.phoenix.converter import transform_metadata
from application_sdk.workflows.transformers.phoenix.schema import PydanticJSONEncoder
from application_sdk.paas.objectstore import ObjectStore
from application_sdk.paas.secretstore import SecretStore

from application_sdk.workflows.sql.utils import prepare_filters
from application_sdk.workflows.utils.activity import auto_heartbeater
from application_sdk.workflows import WorkflowWorkerInterface

logger = logging.getLogger(__name__)


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
    def __init__(self,
        application_name: str = "sql-connector",
        get_sql_engine: Callable[[Dict[str, Any]], Engine] = None,
        TEMPORAL_ACTIVITIES: Sequence[CallableType] = []
    ):
        """
        Initialize the SQL workflow worker.

        :param application_name: The name of the application.
        :param get_sql_engine: A callable that returns an SQLAlchemy engine.
        """
        self.get_sql_engine = get_sql_engine

        if not TEMPORAL_ACTIVITIES:
            # default activities
            self.TEMPORAL_ACTIVITIES = [
                self.setup_output_directory,
                self.fetch_databases,
                self.fetch_schemas,
                self.fetch_tables,
                self.fetch_columns,
                self.teardown_output_directory,
                self.push_results_to_object_store
            ]
        else:
            self.TEMPORAL_ACTIVITIES = TEMPORAL_ACTIVITIES

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

    async def run_query_in_batch(self, connection: Connection, query: str, batch_size: int = 100000):
        """
        Run a query in a batch mode with client-side cursor.

        :param connection: The database connection.
        :param query: The query to run.
        :param batch_size: The batch size.
        :return: The query results.
        :raises Exception: If the query fails.
        """
        loop = asyncio.get_running_loop()
    
        with ThreadPoolExecutor() as pool:
            try:
                cursor = await loop.run_in_executor(pool, connection.execute, text(query))
                column_names: List[str] = []

                while True:
                    rows = await loop.run_in_executor(pool, cursor.fetchmany, batch_size)
                    if not column_names:
                        column_names = [desc[0] for desc in cursor.cursor.description]

                    if not rows:
                        break

                    results = [dict(zip(column_names, row)) for row in rows]
                    yield results
            except Exception as e:
                logger.error(f"Error running query in batch: {e}")
                raise e
        
        logger.info(f"Query execution completed")

    async def run_query_in_batch_with_server_side_cursor(self, connection: Connection, query: str, batch_size: int = 100000):
        """
        Run a query in a batch mode with server-side cursor.

        :param connection: The database connection.
        :param query: The query to run.
        :param batch_size: The batch size.
        :return: The query results.
        :raises Exception: If the query fails.
        """
        loop = asyncio.get_running_loop()

        with ThreadPoolExecutor() as pool:
            try:
            # Use a unique name for the server-side cursor
                cursor_name = f"cursor_{uuid.uuid4()}"
                cursor = await loop.run_in_executor(
                    pool, lambda: connection.cursor(name=cursor_name)
                )

                # Execute the query
                await loop.run_in_executor(pool, cursor.execute, query)
                column_names: List[str] = []

                while True:
                    rows = await loop.run_in_executor(
                        pool, cursor.fetchmany, batch_size
                    )
                    if not column_names:
                        column_names = [desc[0] for desc in cursor.description]

                    if not rows:
                        break

                    results = [dict(zip(column_names, row)) for row in rows]
                    yield results
            except Exception as e:
                logger.error(f"Error running query in batch: {e}")
                raise e
            finally:
                await loop.run_in_executor(pool, cursor.close)
        
        logger.info(f"Query execution completed")

    async def fetch_and_process_data(self, workflow_args: Dict[str, Any], query: str, typename: str):
        """
        Fetch and process data from the database.

        :param workflow_args: The workflow arguments.
        :param query: The query to run.
        :param typename: The type of data to fetch.
        :return: The fetched data.
        :raises Exception: If the data cannot be fetched.
        """
        credentials = SecretStore.extract_credentials(workflow_args["credential_guid"])
        connection = None
        summary = {"raw": 0, "transformed": 0, "errored": 0}
        output_path = workflow_args["output_path"]

        try:
            chunk_number = 0
            engine = self.get_sql_engine(credentials)
            with engine.connect() as connection:
                async for batch in self.run_query_in_batch(connection, query):
                    # Process each batch here
                    await self._process_batch(batch, typename, output_path, summary, chunk_number)
                    chunk_number += 1
            chunk_meta_file = os.path.join(output_path, f"{typename}-chunks.txt")
            async with aiofiles.open(chunk_meta_file, "w") as chunk_meta_f:
                await chunk_meta_f.write(str(chunk_number))
        except Exception as e:
            logger.error(f"Error fetching databases: {e}")
            raise e

        return {typename: summary}

    @staticmethod
    async def _process_batch(
        results: List[Dict[str, Any]],
        typename: str,
        output_path: str,
        summary: Dict[str, int],
        chunk_number: int,
    ) -> None:
        """
        Process a batch of results.

        :param results: The batch of results.
        :param typename: The type of data to fetch.
        :param output_path: The output path.
        :param summary: The summary of the results.
        :param chunk_number: The chunk number.
        :raises Exception: If the results cannot be processed.
        """
        raw_batch: List[str] = []
        transformed_batch: List[str] = []

        for row in results:
            try:
                raw_batch.append(json.dumps(row))
                summary["raw"] += 1

                transformed_data = transform_metadata(
                    "CONNECTOR_NAME", "CONNECTOR_TYPE", typename, row
                )
                if transformed_data is not None:
                    transformed_batch.append(
                        json.dumps(
                            transformed_data.model_dump(), cls=PydanticJSONEncoder
                        )
                    )
                    summary["transformed"] += 1
                else:
                    activity.logger.warning(f"Skipped invalid {typename} data: {row}")
                    summary["errored"] += 1
            except Exception as row_error:
                activity.logger.error(
                    f"Error processing row for {typename}: {row_error}"
                )
                summary["errored"] += 1

        # Write batches to files
        raw_file = os.path.join(output_path, "raw", f"{typename}-{chunk_number}.json")
        transformed_file = os.path.join(
            output_path, "transformed", f"{typename}-{chunk_number}.json"
        )

        async with aiofiles.open(raw_file, "a") as raw_f:
            await raw_f.write("\n".join(raw_batch) + "\n")

        async with aiofiles.open(transformed_file, "a") as trans_f:
            await trans_f.write("\n".join(transformed_batch) + "\n")

    @activity.defn
    @auto_heartbeater
    @staticmethod
    async def fetch_databases(self, workflow_args: Dict[str, Any]):
        """
        Fetch and process databases from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched databases.
        """
        return await self.fetch_and_process_data(workflow_args, self.DATABASE_SQL, "database")

    @activity.defn
    @auto_heartbeater
    @staticmethod
    async def fetch_schemas(self, workflow_args: Dict[str, Any]):
        """
        Fetch and process schemas from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched schemas.
        """
        include_filter = workflow_args.get("metadata", {}).get("include-filter", "{}")
        exclude_filter = workflow_args.get("metadata", {}).get("exclude-filter", "{}")
        temp_table_regex = workflow_args.get("metadata", {}).get("temp-table-regex", "")
        normalized_include_regex, normalized_exclude_regex, _ = (
            prepare_filters(
                include_filter,
                exclude_filter,
                temp_table_regex,
            )
        )
        schema_sql_query = self.SCHEMA_SQL.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
        )
        return await self.fetch_and_process_data(workflow_args, schema_sql_query, "schema")

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
        return await self.fetch_and_process_data(workflow_args, table_sql_query, "table")

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
        return await self.fetch_and_process_data(workflow_args, column_sql_query, "column")

    @activity.defn
    @auto_heartbeater
    @staticmethod
    async def fetch_sql(sql: str):
        """
        Fetch data from the database using a custom SQL query.

        :param sql: The SQL query to run.
        :return: The fetched data.
        """
        pass

    @staticmethod
    @activity.defn
    async def setup_output_directory(output_prefix: str) -> None:
        """
        Setup the output directory.

        :param output_prefix: The output prefix.
        """
        os.makedirs(output_prefix, exist_ok=True)
        os.makedirs(os.path.join(output_prefix, "raw"), exist_ok=True)
        os.makedirs(os.path.join(output_prefix, "transformed"), exist_ok=True)

        activity.logger.info(f"Created output directory: {output_prefix}")

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
        output_path = (
            f"{output_prefix}/{workflow_id}/{workflow_run_id}"
        )
        workflow_args["output_path"] = output_path

        # Create output directory
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            self.setup_output_directory,
            output_path,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=5),
        )

        # run activities in parallel
        activities: List[Coroutine[Any, Any, Any]] = [
            workflow.execute_activity(
                self.fetch_databases,
                workflow_args,
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            ),
            workflow.execute_activity(
                self.fetch_schemas,
                workflow_args,
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            ),
            workflow.execute_activity(
                self.fetch_tables,
                workflow_args,
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            ),
            workflow.execute_activity(
                self.fetch_columns,
                workflow_args,
                retry_policy=retry_policy,
                heartbeat_timeout=timedelta(seconds=10),
                start_to_close_timeout=timedelta(seconds=1000),
            ),
        ]

        # Wait for all activities to complete
        results = await asyncio.gather(*activities)
        extraction_results: Dict[str, Any] = {}
        for result in results:
            extraction_results.update(result)
        
        # Push results to object store
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            self.push_results_to_object_store,
            {"output_prefix": workflow_args["output_prefix"], "output_path": output_path},
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(minutes=10),
        )

        # cleanup output directory
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            self.teardown_output_directory,
            output_path,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=5),
        )
        workflow.logger.info(f"Extraction workflow completed for {workflow_id}")
        workflow.logger.info(f"Extraction results summary: {extraction_results}")


    @staticmethod
    @activity.defn
    async def push_results_to_object_store(output_config: Dict[str, str]) -> None:
        """
        Push the results to the object store.

        :param output_config: The output configuration.
        """
        activity.logger.info("Pushing results to object store")
        try:
            output_prefix, output_path = (
                output_config["output_prefix"],
                output_config["output_path"],
            )
            ObjectStore.push_to_object_store(output_prefix, output_path)
        except Exception as e:
            activity.logger.error(f"Error pushing results to object store: {e}")
            raise e

    @staticmethod
    @activity.defn
    async def teardown_output_directory(output_prefix: str) -> None:
        """
        Teardown the output directory.

        :param output_prefix: The output prefix.
        """
        activity.logger.info(f"Tearing down output directory: {output_prefix}")
        shutil.rmtree(output_prefix)
