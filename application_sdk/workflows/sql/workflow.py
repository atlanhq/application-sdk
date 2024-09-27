
import asyncio
from datetime import timedelta
import shutil
import aiofiles
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import uuid
from typing import Any, Coroutine, Dict, List, Callable

from sqlalchemy import Connection, Engine

from temporalio.common import RetryPolicy
from temporalio import activity, workflow

from application_sdk.workflows.transformers.phoenix.converter import transform_metadata
from application_sdk.workflows.transformers.phoenix.schema import PydanticJSONEncoder
from application_sdk.paas.objectstore import ObjectStore
from application_sdk.workflows.models.workflow import WorkflowConfig
from application_sdk.paas.secretstore import SecretStore

from application_sdk.workflows.sql.utils import prepare_filters
from application_sdk.workflows.utils.activity import auto_heartbeater
from application_sdk.workflows import WorkflowWorkerInterface

logger = logging.getLogger(__name__)


class SQLWorkflowWorkerInterface(WorkflowWorkerInterface):
    DATABASE_SQL = ""
    SCHEMA_SQL = ""
    TABLE_SQL = ""
    COLUMN_SQL = ""


    # Note: the defaults are passed as temporal tries to initialize the workflow with no args
    def __init__(self,
        application_name: str = "sql-connector",
        get_sql_engine: Callable[[Dict[str, Any]], Engine] = None,
    ):
        self.get_sql_engine = get_sql_engine
        self.TEMPORAL_ACTIVITIES = [
            self.setup_output_directory,
            self.fetch_databases,
            self.teardown_output_directory,
            self.push_results_to_object_store
        ]
        super().__init__(application_name)

    async def run_workflow(self, workflow_args: Any) -> Dict[str, Any]:
        credentials = workflow_args["credentials"]
        credential_guid = SecretStore.store_credentials(credentials)
        workflow_args["credentialGuid"] = credential_guid
        return await super().run_workflow(workflow_args)

    @staticmethod
    async def run_query_in_batch(connection: Connection, query: str, batch_size: int = 100000):
        """
        Run a query in a batch mode with server-side cursor.
        """
        loop = asyncio.get_running_loop()

        with ThreadPoolExecutor() as pool:
            # Use a unique name for the server-side cursor
            cursor_name = f"cursor_{uuid.uuid4()}"
            cursor = await loop.run_in_executor(
                pool, lambda: connection.cursor(name=cursor_name)
            )

            try:
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

    async def fetch_and_process_data(self, config: WorkflowConfig, query: str):
        credentials = SecretStore.extract_credentials(config.credentialsGUID)
        connection = None
        summary = {"raw": 0, "transformed": 0, "errored": 0}
        chunk_number = 0
        typename = "database"
        output_path = config.outputPath

        try:
            connection = self.get_sql_engine(credentials.model_dump())
            async for batch in SQLWorkflowWorkerInterface.run_query_in_batch(connection, query):
                # Process each batch here
                await SQLWorkflowWorkerInterface._process_batch(batch, typename, output_path, summary, chunk_number)
                chunk_number += 1
            
            chunk_meta_file = os.path.join(output_path, f"{typename}-chunks.txt")
            async with aiofiles.open(chunk_meta_file, "w") as chunk_meta_f:
                await chunk_meta_f.write(str(chunk_number))
        except Exception as e:
            logger.error(f"Error fetching databases: {e}")
            raise e
        finally:
            if connection:
                connection.close()

        return summary

    @staticmethod
    async def _process_batch(
        results: List[Dict[str, Any]],
        typename: str,
        output_path: str,
        summary: Dict[str, int],
        chunk_number: int,
    ) -> None:
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
    async def fetch_databases(workflow_args: Dict[str, Any]):
        config = WorkflowConfig(**workflow_args["config"])
        sql_query = workflow_args["sql_query"]
        return await SQLWorkflowWorkerInterface.fetch_and_process_data(config, sql_query)

    @activity.defn
    @auto_heartbeater
    @staticmethod
    async def fetch_schemas(self, credentialGuid: str, config: WorkflowConfig):
        normalized_include_regex, normalized_exclude_regex, exclude_table = (
            prepare_filters(
                config.includeFilterStr,
                config.excludeFilterStr,
                config.tempTableRegexStr,
            )
        )
        schema_sql_query = SQLWorkflowWorkerInterface.SCHEMA_SQL.format(
                normalized_include_regex=normalized_include_regex,
                normalized_exclude_regex=normalized_exclude_regex,
            )
        return await self.fetch_and_process_data(credentialGuid, schema_sql_query)

    @activity.defn
    @auto_heartbeater
    async def fetch_tables(self):
        pass

    @activity.defn
    @auto_heartbeater
    async def fetch_columns(self):
        pass

    @activity.defn
    @auto_heartbeater
    @staticmethod
    async def fetch_sql(sql: str):
        pass

    @staticmethod
    @activity.defn
    async def setup_output_directory(output_prefix: str) -> None:
        os.makedirs(output_prefix, exist_ok=True)
        os.makedirs(os.path.join(output_prefix, "raw"), exist_ok=True)
        os.makedirs(os.path.join(output_prefix, "transformed"), exist_ok=True)

        activity.logger.info(f"Created output directory: {output_prefix}")

    @workflow.run
    async def run(self, config: WorkflowConfig):
        workflow.logger.info(f"Starting extraction workflow for {config.workflowId}")
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

        workflow_run_id = workflow.info().run_id
        config.outputPath = (
            f"{config.outputPrefix}/{config.workflowId}/{workflow_run_id}"
        )

        # Create output directory
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            SQLWorkflowWorkerInterface.setup_output_directory,
            config.outputPath,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=5),
        )

        # run activities in parallel
        activities: List[Coroutine[Any, Any, Any]] = []
        logger.info(f"Fetching databases: {self.DATABASE_SQL}")
        activities.append(
            workflow.execute_activity(
                SQLWorkflowWorkerInterface.fetch_databases,
                {"config": config, "sql_query": self.DATABASE_SQL},
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            )
        )

        # Wait for all activities to complete
        results = await asyncio.gather(*activities)
        extraction_results: Dict[str, Any] = {}
        for result in results:
            extraction_results.update(result)
        
        # Push results to object store
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            SQLWorkflowWorkerInterface.push_results_to_object_store,
            {"output_prefix": config.outputPrefix, "output_path": config.outputPath},
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(minutes=10),
        )

        # cleanup output directory
        await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            SQLWorkflowWorkerInterface.teardown_output_directory,
            config.outputPath,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=5),
        )
        workflow.logger.info(f"Extraction workflow completed for {config.workflowId}")
        workflow.logger.info(f"Extraction results summary: {extraction_results}")


    @staticmethod
    @activity.defn
    async def push_results_to_object_store(output_config: Dict[str, str]) -> None:
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
        activity.logger.info(f"Tearing down output directory: {output_prefix}")
        shutil.rmtree(output_prefix)
