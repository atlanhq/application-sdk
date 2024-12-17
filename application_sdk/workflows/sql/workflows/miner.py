import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, List

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from application_sdk.inputs.objectstore import ObjectStore
from application_sdk.inputs.statestore import StateStore
from application_sdk.outputs.json import JSONChunkedObjectStoreWriter
from application_sdk.workflows.resources.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)
from application_sdk.workflows.sql.resources.sql_resource import (
    SQLResource,
    SQLResourceConfig,
)
from application_sdk.workflows.utils.activity import auto_heartbeater
from application_sdk.workflows.workflow import WorkflowInterface

logger = logging.getLogger(__name__)


class MinerArgs(BaseModel):
    database_name_cleaned: str
    schema_name_cleaned: str
    timestamp_column: str
    chunk_size: int
    current_marker: int
    sql_replace_from: str
    sql_replace_to: str
    ranged_sql_start_key: str
    ranged_sql_end_key: str
    miner_start_time_epoch: int = Field(
        default_factory=lambda: int((datetime.now() - timedelta(days=14)).timestamp())
    )


@workflow.defn
class SQLMinerWorkflow(WorkflowInterface):
    fetch_queries_sql = ""

    sql_resource: SQLResource | None = None

    application_name: str = "sql-miner"
    batch_size: int = 100000

    # Note: the defaults are passed as temporal tries to initialize the workflow with no args
    def __init__(self):
        super().__init__()

    def set_sql_resource(self, sql_resource: SQLResource) -> "SQLMinerWorkflow":
        self.sql_resource = sql_resource
        return self

    def set_application_name(self, application_name: str) -> "SQLMinerWorkflow":
        self.application_name = application_name
        return self

    def set_batch_size(self, batch_size: int) -> "SQLMinerWorkflow":
        self.batch_size = batch_size
        return self

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
    ) -> "SQLMinerWorkflow":
        super().set_temporal_resource(temporal_resource)
        return self

    def get_activities(self) -> List[Callable[..., Any]]:
        return [
            self.get_query_batches,
            self.fetch_queries,
        ] + super().get_activities()

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
                async for batch in self.sql_resource.run_query(query, self.batch_size):
                    # Write raw data
                    await raw_writer.write_list(batch)

                return await raw_writer.close()

        except Exception as e:
            logger.error(f"Error fetching queries: {e}")
            raise e

    @activity.defn
    @auto_heartbeater
    async def fetch_queries(self, workflow_args: Dict[str, Any]):
        """
        Fetch and process queries from the database.

        :param workflow_args: The workflow arguments.
        :return: The fetched queries.
        """
        assert "sql_query" in workflow_args, "sql_query is required"

        await self.fetch_data(workflow_args, workflow_args["sql_query"], "queries")

    async def parallelize_query(
        self,
        query: str,
        timestamp_column: str,
        chunk_size: int,
        current_marker: str,
        sql_ranged_replace_from: str,
        sql_ranged_replace_to: str,
        ranged_sql_start_key: str,
        ranged_sql_end_key: str,
    ):
        """
        Processes a single chunk of the query, collecting timestamp ranges.

        Args:
            query: The SQL query to process
            timestamp_column: Column name containing the timestamp
            chunk_size: Number of records per chunk
            current_marker: Starting timestamp marker
            sql_ranged_replace_from: Original SQL fragment to replace
            sql_ranged_replace_to: SQL fragment with range placeholders
            ranged_sql_start_key: Placeholder for range start timestamp
            ranged_sql_end_key: Placeholder for range end timestamp
            parallel_markers: List to store the chunked queries

        Returns:
            Tuple of (final chunk count, records in last chunk)
        """
        if chunk_size <= 0:
            raise ValueError("Chunk size must be greater than 0")

        parallel_markers: List[Dict[str, Any]] = []

        marked_sql = query.replace(ranged_sql_start_key, current_marker)
        rewritten_query = f"WITH T AS ({marked_sql}) SELECT {timestamp_column} FROM T ORDER BY {timestamp_column} ASC"
        logger.info(f"Executing query: {rewritten_query}")

        chunk_start_marker = None
        chunk_end_marker = None
        record_count = 0
        last_marker = None

        if not self.sql_resource:
            raise ValueError("SQL resource is not initialized")

        async for result_batch in self.sql_resource.run_query(rewritten_query):
            for row in result_batch:
                timestamp = row[timestamp_column.lower()]
                new_marker = str(int(timestamp.timestamp() * 1000))

                if last_marker == new_marker:
                    logger.info("Skipping duplicate start time")
                    record_count += 1
                    continue

                if not chunk_start_marker:
                    chunk_start_marker = new_marker
                chunk_end_marker = new_marker
                record_count += 1
                last_marker = new_marker

                if record_count >= chunk_size:
                    self._create_chunked_query(
                        query=query,
                        start_marker=chunk_start_marker,
                        end_marker=chunk_end_marker,
                        parallel_markers=parallel_markers,
                        record_count=record_count,
                        sql_ranged_replace_from=sql_ranged_replace_from,
                        sql_ranged_replace_to=sql_ranged_replace_to,
                        ranged_sql_start_key=ranged_sql_start_key,
                        ranged_sql_end_key=ranged_sql_end_key,
                    )
                    record_count = 0
                    chunk_start_marker = None
                    chunk_end_marker = None

        if record_count > 0:
            self._create_chunked_query(
                query=query,
                start_marker=chunk_start_marker,
                end_marker=chunk_end_marker,
                parallel_markers=parallel_markers,
                record_count=record_count,
                sql_ranged_replace_from=sql_ranged_replace_from,
                sql_ranged_replace_to=sql_ranged_replace_to,
                ranged_sql_start_key=ranged_sql_start_key,
                ranged_sql_end_key=ranged_sql_end_key,
            )

        logger.info(f"Parallelized queries into {len(parallel_markers)} chunks")

        return parallel_markers

    def _create_chunked_query(
        self,
        query: str,
        start_marker: str | None,
        end_marker: str | None,
        parallel_markers: List[Dict[str, Any]],
        record_count: int,
        sql_ranged_replace_from: str,
        sql_ranged_replace_to: str,
        ranged_sql_start_key: str,
        ranged_sql_end_key: str,
    ) -> None:
        """
        Creates a chunked query with the specified time range and adds it to parallel_markers.

        Args:
            query: The base SQL query
            chunk_count: Current chunk number
            start_marker: Start timestamp for the chunk
            end_marker: End timestamp for the chunk
            parallel_markers: List to store the chunked queries
            record_count: Number of records in this chunk
            sql_ranged_replace_from: Original SQL fragment to replace
            sql_ranged_replace_to: SQL fragment with range placeholders
            ranged_sql_start_key: Placeholder for range start timestamp
            ranged_sql_end_key: Placeholder for range end timestamp
        """
        if not start_marker or not end_marker:
            return

        chunked_sql = query.replace(
            sql_ranged_replace_from,
            sql_ranged_replace_to.replace(ranged_sql_start_key, start_marker).replace(
                ranged_sql_end_key, end_marker
            ),
        )

        logger.info(
            f"Processed {record_count} records in chunk {len(parallel_markers)}, "
            f"with start marker {start_marker} and end marker {end_marker}"
        )
        logger.info(f"Chunked SQL: {chunked_sql}")

        parallel_markers.append(
            {
                "sql": chunked_sql,
                "start": start_marker,
                "end": end_marker,
                "count": record_count,
            }
        )

    @activity.defn
    @auto_heartbeater
    async def get_query_batches(
        self, workflow_args: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        miner_args = MinerArgs(**workflow_args.get("miner_args", {}))

        queries_sql_query = self.fetch_queries_sql.format(
            database_name_cleaned=miner_args.database_name_cleaned,
            schema_name_cleaned=miner_args.schema_name_cleaned,
            miner_start_time_epoch=miner_args.miner_start_time_epoch,
        )

        try:
            parallel_markers = await self.parallelize_query(
                query=queries_sql_query,
                timestamp_column=miner_args.timestamp_column,
                chunk_size=miner_args.chunk_size,
                current_marker=str(miner_args.current_marker),
                sql_ranged_replace_from=miner_args.sql_replace_from,
                sql_ranged_replace_to=miner_args.sql_replace_to,
                ranged_sql_start_key=miner_args.ranged_sql_start_key,
                ranged_sql_end_key=miner_args.ranged_sql_end_key,
            )
        except Exception as e:
            logger.error(f"Failed to parallelize queries: {e}")
            raise e

        logger.info(f"Parallelized queries into {len(parallel_markers)} chunks")

        # Write the results to a metadata file
        output_path = os.path.join(workflow_args["output_path"], "raw")
        metadata_file_path = os.path.join(output_path, "queries_metadata.json")
        os.makedirs(os.path.dirname(metadata_file_path), exist_ok=True)
        with open(metadata_file_path, "w") as f:
            f.write(json.dumps(parallel_markers))

        await ObjectStore.push_file_to_object_store(
            workflow_args["output_prefix"], metadata_file_path
        )

        return parallel_markers

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]):
        """
        Run the workflow.

        :param workflow_args: The workflow arguments.
        """
        workflow_guid = workflow_config["workflow_id"]
        workflow_args = StateStore.extract_configuration(workflow_guid)

        if not self.sql_resource:
            credentials = StateStore.extract_credentials(
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

        results: List[Dict[str, Any]] = await workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
            self.get_query_batches,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        miner_activities: List[Coroutine[Any, Any, None]] = []

        # Extract Queries
        for result in results:
            activity_args = workflow_args.copy()
            activity_args["sql_query"] = result["sql"]
            activity_args["start_marker"] = result["start"]
            activity_args["end_marker"] = result["end"]

            miner_activities.append(
                workflow.execute_activity(  # pyright: ignore[reportUnknownMemberType]
                    self.fetch_queries,
                    activity_args,
                    retry_policy=retry_policy,
                    start_to_close_timeout=timedelta(seconds=1000),
                )
            )

        await asyncio.gather(*miner_activities)

        workflow.logger.info(f"Miner workflow completed for {workflow_id}")
