import asyncio
import logging
from datetime import timedelta
from typing import Any, Callable, Coroutine, Dict, List, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities
from application_sdk.clients.sql_client import SQLClient, SQLClientConfig
from application_sdk.clients.temporal_client import TemporalClient, TemporalConfig
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.statestore import StateStore
from application_sdk.workflows import WorkflowInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


@workflow.defn
class SQLQueryExtractionWorkflow(WorkflowInterface):
    activities_class: Type[SQLQueryExtractionActivities] = SQLQueryExtractionActivities

    fetch_queries_sql = ""

    sql_client: SQLClient | None = None

    application_name: str = "sql-miner"
    batch_size: int = 100000

    # Note: the defaults are passed as temporal tries to initialize the workflow with no args

    @staticmethod
    def get_activities(
        activities: ActivitiesInterface,
    ) -> Sequence[Callable[..., Any]]:
        return [
            activities.get_query_batches,
            activities.fetch_queries,
            activities.preflight_check,
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]):
        """
        Run the workflow.

        :param workflow_args: The workflow arguments.
        """
        workflow_guid = workflow_config["workflow_id"]
        workflow_args = StateStore.extract_configuration(workflow_guid)

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

        await workflow.execute_activity_method(
            self.activities_class.preflight_check,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        results: List[Dict[str, Any]] = await workflow.execute_activity_method(  # pyright: ignore[reportUnknownMemberType]
            self.activities_class.get_query_batches,
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
                    self.activities_class.fetch_queries,
                    activity_args,
                    retry_policy=retry_policy,
                    start_to_close_timeout=timedelta(seconds=1000),
                )
            )

        await asyncio.gather(*miner_activities)

        workflow.logger.info(f"Miner workflow completed for {workflow_id}")
