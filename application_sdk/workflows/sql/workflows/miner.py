import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, List

import pandas as pd
from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from application_sdk import activity_pd
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.objectstore import ObjectStore
from application_sdk.inputs.statestore import StateStore
from application_sdk.outputs.json import JsonOutput
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

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


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


def incremental_miner_decorator(fetch_queries_sql: str):
    """
    High-level decorator that encapsulates the entire incrementalmining workflow including run method
    and all associated functions.

    Args:
        fetch_queries_sql: The base SQL query template to be executed
    """

    def decorator(cls):
        # Store original __init__
        original_init = cls.__init__

        def __init__(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            self.fetch_queries_sql = fetch_queries_sql
            self.batch_size = 100000

        @activity.defn
        @auto_heartbeater
        @activity_pd(
            batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
                engine=self.sql_resource.engine, query=workflow_args["sql_query"]
            ),
            raw_output=lambda self, workflow_args: JsonOutput(
                output_path=f"{workflow_args['output_path']}/raw/query",
                upload_file_prefix=workflow_args["output_prefix"],
                path_gen=lambda chunk_start,
                chunk_count: f"{workflow_args['start_marker']}_{workflow_args['end_marker']}.json",
            ),
        )
        async def fetch_queries(
            self, batch_input: pd.DataFrame, raw_output: JsonOutput, **kwargs
        ):
            """
            Fetch and process queries from the database.
            Returns the end marker timestamp for this chunk.
            """
            await raw_output.write_df(batch_input)
            return kwargs.get("end_marker", None)

        async def _create_chunked_query(
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
            if not start_marker or not end_marker:
                return

            chunked_sql = query.replace(
                sql_ranged_replace_from,
                sql_ranged_replace_to.replace(
                    ranged_sql_start_key, start_marker
                ).replace(ranged_sql_end_key, end_marker),
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
                        await self._create_chunked_query(
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
                await self._create_chunked_query(
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

            return parallel_markers

        @activity.defn
        @auto_heartbeater
        async def get_query_batches(
            self, workflow_args: Dict[str, Any]
        ) -> List[Dict[str, Any]]:
            workflow_guid = workflow_args["workflow_id"]
            miner_args = MinerArgs(**workflow_args.get("miner_args", {}))

            # Try to get last processed timestamp from StateStore
            try:
                last_processed_timestamp = StateStore.extract_last_processed_timestamp(
                    workflow_guid
                )
                if last_processed_timestamp:
                    # Convert datetime to epoch milliseconds
                    miner_args.miner_start_time_epoch = int(
                        last_processed_timestamp.timestamp() * 1000
                    )
                    logger.info(
                        f"Using last processed timestamp from state store: {last_processed_timestamp}"
                    )
            except Exception as e:
                logger.info(f"No last processed timestamp found in state store: {e}")
                # If no last processed timestamp, use the one from miner_args if present
                if not miner_args.miner_start_time_epoch:
                    # Default to 2 weeks ago if no timestamp in miner_args
                    miner_args.miner_start_time_epoch = int(
                        (datetime.now() - timedelta(days=14)).timestamp() * 1000
                    )
                    logger.info(
                        f"Using default start time (2 weeks ago): {datetime.fromtimestamp(miner_args.miner_start_time_epoch/1000)}"
                    )
                else:
                    logger.info(
                        f"Using start time from miner_args: {datetime.fromtimestamp(miner_args.miner_start_time_epoch/1000)}"
                    )

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
            output_path = os.path.join(workflow_args["output_path"], "raw", "query")
            metadata_file_path = os.path.join(output_path, "metadata.json")
            os.makedirs(os.path.dirname(metadata_file_path), exist_ok=True)
            with open(metadata_file_path, "w") as f:
                f.write(json.dumps(parallel_markers))

            await ObjectStore.push_file_to_object_store(
                workflow_args["output_prefix"], metadata_file_path
            )

            return parallel_markers

        @workflow.run
        async def run(self, workflow_config: Dict[str, Any]):
            workflow_guid = workflow_config["workflow_id"]
            workflow_args = StateStore.extract_configuration(workflow_guid)

            if not self.sql_resource:
                credentials = StateStore.extract_credentials(
                    workflow_args["credential_guid"]
                )
                self.sql_resource = SQLResource(
                    SQLResourceConfig(credentials=credentials)
                )

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

            await workflow.execute_activity(
                self.preflight_check,
                workflow_args,
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            )

            results: List[Dict[str, Any]] = await workflow.execute_activity(
                self.get_query_batches,
                workflow_args,
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            )

            miner_activities: List[Coroutine[Any, Any, str]] = []

            # Extract Queries
            for result in results:
                activity_args = workflow_args.copy()
                activity_args["sql_query"] = result["sql"]
                activity_args["start_marker"] = result["start"]
                activity_args["end_marker"] = result["end"]

                miner_activities.append(
                    workflow.execute_activity(
                        self.fetch_queries,
                        activity_args,
                        retry_policy=retry_policy,
                        start_to_close_timeout=timedelta(seconds=1000),
                    )
                )

            # Collect all end timestamps from parallel activities
            last_timestamps = await asyncio.gather(*miner_activities)

            # Find the most recent timestamp
            latest_timestamp = max(last_timestamps, key=int)

            # Convert timestamp to datetime and store it
            latest_datetime = datetime.fromtimestamp(
                int(latest_timestamp) / 1000
            )  # Convert milliseconds to seconds
            StateStore.store_last_processed_timestamp(latest_datetime, workflow_guid)

            workflow.logger.info(f"Miner workflow completed for {workflow_id}")
            workflow.logger.info(f"Latest processed timestamp: {latest_datetime}")

        # Add methods to the class
        cls.__init__ = __init__
        cls._create_chunked_query = _create_chunked_query
        cls.parallelize_query = parallelize_query
        cls.get_query_batches = get_query_batches
        cls.fetch_queries = fetch_queries
        cls.run = run

        return cls

    return decorator


@incremental_miner_decorator(fetch_queries_sql="")
class SQLMinerWorkflow(WorkflowInterface):
    sql_resource: SQLResource | None = None
    application_name: str = "sql-miner"

    def __init__(self):
        super().__init__()

    def set_sql_resource(self, sql_resource: SQLResource) -> "SQLMinerWorkflow":
        self.sql_resource = sql_resource
        return self

    def set_application_name(self, application_name: str) -> "SQLMinerWorkflow":
        self.application_name = application_name
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
        if self.sql_resource is None:
            raise ValueError("SQL resource is not set")

        self.sql_resource.set_credentials(workflow_args["credentials"])
        await self.sql_resource.load()

        workflow_class = workflow_class or self.__class__

        return await super().start(workflow_args, workflow_class)

    @activity.defn(name="miner_preflight_check")
    @auto_heartbeater
    async def preflight_check(self, workflow_args: Dict[str, Any]):
        result = await self.preflight_check_controller.preflight_check(
            {
                "form_data": workflow_args["metadata"],
            }
        )
        if not result or "error" in result:
            raise ValueError("Preflight check failed")
