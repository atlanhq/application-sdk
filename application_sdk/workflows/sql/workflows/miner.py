import asyncio
import json
import logging
import os
from datetime import timedelta
from typing import Any, Callable, Coroutine, Dict, List

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from application_sdk.common.query_parallelizer import ParallelizeQueryExecutor
from application_sdk.paas.objectstore import ObjectStore
from application_sdk.paas.secretstore import SecretStore
from application_sdk.paas.writers.json import JSONChunkedObjectStoreWriter
from application_sdk.workflows.resources.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)
from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow

# from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.utils.activity import auto_heartbeater

logger = logging.getLogger(__name__)


@workflow.defn
class SQLMinerWorkflow(SQLWorkflow):
    fetch_queries_sql = ""

    def get_activities(self) -> List[Callable[..., Any]]:
        return [
            self.get_query_batches,
            self.fetch_queries,
        ]

    def store_credentials(self, credentials: Dict[str, Any]) -> str:
        return SecretStore.store_credentials(credentials)

    # def get_parallelized_chunks()

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
        start_marker = workflow_args["start_marker"]
        end_marker = workflow_args["end_marker"]

        raw_files_prefix = os.path.join(
            output_path, "raw", f"{typename}_{start_marker}_{end_marker}"
        )
        raw_files_output_prefix = workflow_args["output_prefix"]

        try:
            async with (
                JSONChunkedObjectStoreWriter(
                    raw_files_prefix,
                    raw_files_output_prefix,
                    chunk_size=-1,  # -1 means no chunking
                ) as raw_writer,
            ):
                if self.sql_resource is None:
                    raise ValueError("SQL resource is not set")

                async for batch in self.sql_resource.run_query(query, self.batch_size):
                    # Write raw data
                    await raw_writer.write_list(batch)

                return await raw_writer.close()

        except Exception as e:
            logger.error(f"Error fetching databases: {e}")
            raise e

    @activity.defn
    @auto_heartbeater
    async def fetch_queries(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fetch and process queries from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched queries.
        """
        sql_query = workflow_args.get("sql_query", "")
        # logger.info(f"Fetching queries: {workflow_args.get('chunk_index', 0)}")

        chunk_count = await self.fetch_data(workflow_args, sql_query, "queries")
        return {"typename": "queries", "chunk_count": chunk_count}

    @activity.defn
    @auto_heartbeater
    async def get_query_batches(
        self, workflow_args: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        miner_args = workflow_args.get("miner_args", {})

        queries_sql_query = self.fetch_queries_sql.format(
            database_name_cleaned=miner_args.get("database_name_cleaned", "SNOWFLAKE"),
            schema_name_cleaned=miner_args.get("schema_name_cleaned", "ACCOUNT_USAGE"),
            mining_start_date=miner_args.get("mining_start_date", "1731723638"),
            crawler_last_run=miner_args.get("crawler_last_run", "1731723638"),
        )

        executor = ParallelizeQueryExecutor(
            query=queries_sql_query,
            timestamp_column=miner_args.get("timestamp_column", "START_TIME"),
            chunk_size=miner_args.get("chunk_size", 200),
            current_marker=miner_args.get("current_marker", "1731723638"),
            sql_ranged_replace_from=miner_args.get("sql_replace_from", ""),
            sql_ranged_replace_to=miner_args.get("sql_replace_to", ""),
            ranged_sql_start_key=miner_args.get(
                "ranged_sql_start_key", "[START_MARKER]"
            ),
            ranged_sql_end_key=miner_args.get("ranged_sql_end_key", "[END_MARKER]"),
            sql_resource=self.sql_resource,
        )

        results = await executor.execute()

        # Write the results to a metadata file
        output_path = os.path.join(workflow_args["output_path"], "raw")
        metadata_file_path = os.path.join(output_path, "queries_metadata.json")
        os.makedirs(os.path.dirname(metadata_file_path), exist_ok=True)
        with open(metadata_file_path, "w") as f:
            f.write(json.dumps(results))

        await ObjectStore.push_file_to_object_store(
            workflow_args["output_prefix"], metadata_file_path
        )

        return results

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
            self.sql_resource = SQLResource(SQLResourceConfig(credentials=credentials))

        if not self.temporal_resource:
            self.temporal_resource = TemporalResource(
                TemporalConfig(application_name=self.application_name)
            )

        workflow_id = workflow_args["workflow_id"]
        workflow.logger.info(f"Starting miner workflow for {workflow_id}")
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

        workflow_run_id = workflow.info().run_id
        output_prefix = workflow_args["output_prefix"]
        output_path = f"{output_prefix}/{workflow_id}/{workflow_run_id}"
        workflow_args["output_path"] = output_path
        workflow_args["chunk_size"] = 200

        results: List[Dict[str, Any]] = await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            self.get_query_batches,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        miner_activities: List[Coroutine[Any, Any, Dict[str, Any]]] = []

        # Extract Queries
        for result in results:
            # Create a new copy of workflow_args for each activity
            activity_args = workflow_args.copy()
            activity_args["sql_query"] = result["sql"]
            activity_args["start_marker"] = result["start"]
            activity_args["end_marker"] = result["end"]

            miner_activities.append(
                workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
                    self.fetch_queries,
                    activity_args,  # Pass the copied args
                    retry_policy=retry_policy,
                    start_to_close_timeout=timedelta(seconds=1000),
                )
            )

        await asyncio.gather(*miner_activities)

        workflow.logger.info(f"Miner workflow completed for {workflow_id}")
