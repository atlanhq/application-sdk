import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List

import pandas as pd
from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from application_sdk import activity_pd
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.decorators.query_batching import incremental_query_batching
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


@workflow.defn
class SQLMinerWorkflow(WorkflowInterface):
    fetch_queries_sql = ""

    sql_resource: SQLResource | None = None

    application_name: str = "sql-miner"
    batch_size: int = 100000

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
        return [self.fetch_queries] + super().get_activities()

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

        :param workflow_args: The workflow arguments.
        :return: The end marker timestamp for this chunk.
        """
        await raw_output.write_df(batch_input)
        return kwargs.get("end_marker", 0)

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

    @incremental_query_batching(
        workflow_id=lambda workflow_config: workflow_config["workflow_id"],
        query=lambda workflow_config: StateStore.extract_configuration(
            workflow_config["workflow_id"]
        )["miner_args"]["query"],
        timestamp_column=lambda workflow_config: StateStore.extract_configuration(
            workflow_config["workflow_id"]
        )["miner_args"]["timestamp_column"],
        chunk_size=lambda workflow_config: StateStore.extract_configuration(
            workflow_config["workflow_id"]
        )["miner_args"]["chunk_size"],
        sql_ranged_replace_from=lambda workflow_config: StateStore.extract_configuration(
            workflow_config["workflow_id"]
        )["miner_args"]["sql_replace_from"],
        sql_ranged_replace_to=lambda workflow_config: StateStore.extract_configuration(
            workflow_config["workflow_id"]
        )["miner_args"]["sql_replace_to"],
        ranged_sql_start_key=lambda workflow_config: StateStore.extract_configuration(
            workflow_config["workflow_id"]
        )["miner_args"]["ranged_sql_start_key"],
        ranged_sql_end_key=lambda workflow_config: StateStore.extract_configuration(
            workflow_config["workflow_id"]
        )["miner_args"]["ranged_sql_end_key"],
    )
    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]):
        """
        Run the workflow.

        :param workflow_args: The workflow arguments.
        """
        workflow_guid = workflow_config["workflow_id"]
        workflow_args = StateStore.extract_configuration(workflow_guid)

        # Format and store the query in miner_args
        workflow_args["miner_args"]["query"] = self.fetch_queries_sql.format(
            database_name_cleaned=workflow_args["miner_args"]["database_name_cleaned"],
            schema_name_cleaned=workflow_args["miner_args"]["schema_name_cleaned"],
            miner_start_time_epoch=workflow_args["miner_args"][
                "miner_start_time_epoch"
            ],
        )
        StateStore.store_configuration(workflow_guid, workflow_args)

        if not self.sql_resource:
            credentials = StateStore.extract_credentials(
                workflow_args["credential_guid"]
            )
            self.sql_resource = SQLResource(SQLResourceConfig(credentials=credentials))

        if not self.temporal_resource:
            self.temporal_resource = TemporalResource(
                TemporalConfig(application_name=self.application_name)
            )

        # Add sql_resource to workflow_config for the decorator
        workflow_config["sql_resource"] = self.sql_resource

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

        # Execute queries in parallel using the parallel markers from state store
        parallel_markers = StateStore.extract_configuration(
            f"parallel_markers_{workflow_id}"
        )["markers"]
        miner_activities = []

        # Extract Queries
        for marker in parallel_markers:
            activity_args = workflow_args.copy()
            activity_args["sql_query"] = marker["sql"]
            activity_args["start_marker"] = marker["start"]
            activity_args["end_marker"] = marker["end"]

            miner_activities.append(
                workflow.execute_activity(
                    self.fetch_queries,
                    activity_args,
                    retry_policy=retry_policy,
                    start_to_close_timeout=timedelta(seconds=1000),
                )
            )

        # Execute all activities in parallel and collect end markers
        end_markers = await asyncio.gather(*miner_activities)

        # Find the latest timestamp and store it
        if end_markers:
            latest_end_marker = max(int(marker) for marker in end_markers if marker)
            latest_datetime = datetime.fromtimestamp(latest_end_marker / 1000)
            StateStore.store_last_processed_timestamp(latest_datetime, workflow_id)
            workflow.logger.info(f"Stored last processed timestamp: {latest_datetime}")

        workflow.logger.info(f"Miner workflow completed for {workflow_id}")
