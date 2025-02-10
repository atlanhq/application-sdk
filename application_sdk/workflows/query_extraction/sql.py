"""SQL query extraction workflow implementation.

This module provides the workflow implementation for extracting SQL queries from
databases. It handles batch processing of queries and manages the workflow state.
"""

import asyncio
from datetime import timedelta
from typing import Any, Callable, Coroutine, Dict, List, Sequence, Type

import uvloop
from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities
from application_sdk.clients.sql import SQLClient
from application_sdk.common.constants import ApplicationConstants
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.workflows.query_extraction import QueryExtractionWorkflow

logger = get_logger(__name__)
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


@workflow.defn
class SQLQueryExtractionWorkflow(QueryExtractionWorkflow):
    """SQL query extraction workflow implementation.

    This class implements the workflow for extracting SQL queries from databases.
    It handles batch processing of queries and manages the workflow state.

    Attributes:
        activities_cls (Type[SQLQueryExtractionActivities]): The activities class
            for SQL query extraction.
        fetch_queries_sql (str): SQL query for fetching queries.
        sql_client (SQLClient | None): SQL client instance.
        application_name (str): Name of the application.
        batch_size (int): Size of each batch for processing.
    """

    activities_cls: Type[SQLQueryExtractionActivities] = SQLQueryExtractionActivities

    fetch_queries_sql = ""

    sql_client: SQLClient | None = None

    application_name: str = ApplicationConstants.APPLICATION_NAME.value
    batch_size: int = 100000

    # Note: the defaults are passed as temporal tries to initialize the workflow with no args

    @staticmethod
    def get_activities(
        activities: ActivitiesInterface,
    ) -> Sequence[Callable[..., Any]]:
        """Get the sequence of activities for this workflow.

        Args:
            activities (ActivitiesInterface): The activities interface instance.

        Returns:
            Sequence[Callable[..., Any]]: List of activity methods to be executed.
        """
        return [
            activities.get_query_batches,
            activities.fetch_queries,
            activities.preflight_check,
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]):
        """Run the workflow.

        Args:
            workflow_config (Dict[str, Any]): The workflow configuration dictionary.

        Returns:
            None
        """
        await super().run(workflow_config)

        workflow_guid = workflow_config["workflow_id"]
        workflow_args = StateStoreInput.extract_configuration(workflow_guid)

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

        results: List[Dict[str, Any]] = await workflow.execute_activity_method(  # pyright: ignore[reportUnknownMemberType]
            self.activities_cls.get_query_batches,
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
                    self.activities_cls.fetch_queries,
                    activity_args,
                    retry_policy=retry_policy,
                    start_to_close_timeout=timedelta(seconds=1000),
                )
            )

        await asyncio.gather(*miner_activities)

        workflow.logger.info(f"Miner workflow completed for {workflow_id}")
