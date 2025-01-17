import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Type

import pandas as pd
from pydantic import BaseModel, Field
from temporalio import activity

from application_sdk import activity_pd
from application_sdk.activities import ActivitiesInterface
from application_sdk.clients.sql import SQLClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.handlers.sql import SQLHandler
from application_sdk.inputs.objectstore import ObjectStore
from application_sdk.outputs.json import JsonOutput
from application_sdk.workflows.utils.activity import auto_heartbeater

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


class SQLQueryExtractionActivities(ActivitiesInterface):
    _state: Dict[str, Any] = {}

    sql_client_class: Type[SQLClient] = SQLClient
    handler_class: Type[SQLHandler] = SQLHandler

    def __init__(
        self,
        sql_client_class: Type[SQLClient] = SQLClient,
        handler_class: Type[SQLHandler] = SQLHandler,
    ):
        self.sql_client_class = sql_client_class
        self.handler_class = handler_class

        super().__init__()

    @activity.defn
    @auto_heartbeater
    @activity_pd(
        batch_input=lambda self,
        workflow_args,
        state,
        activity_input,
        **kwargs: self.sql_client.sql_input(
            engine=state["sql_client"].engine, query=activity_input["sql_query"]
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

        :param workflow_args: The workflow arguments.
        :return: The fetched queries.
        """
        await raw_output.write_df(batch_input)

    @activity.defn
    @auto_heartbeater
    async def get_query_batches(
        self, workflow_args: Dict[str, Any], activity_input: Dict[str, Any], **kwargs
    ) -> List[Dict[str, Any]]:
        miner_args = MinerArgs(**workflow_args.get("miner_args", {}))

        queries_sql_query = activity_input["sql_query"].format(
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

    @activity.defn(name="miner_preflight_check")
    @auto_heartbeater
    async def miner_preflight_check(self, workflow_args: Dict[str, Any]):
        state = await self._get_state()

        result = await state["handler"].preflight_check(
            {
                "metadata": workflow_args["metadata"],
            }
        )
        if not result or "error" in result:
            raise ValueError("Preflight check failed")
