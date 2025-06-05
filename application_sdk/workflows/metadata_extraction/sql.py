"""SQL metadata extraction workflow implementation.

This module provides the workflow implementation for extracting metadata from SQL databases,
including databases, schemas, tables, and columns.
"""

import asyncio
from time import time
from typing import Any, Callable, Coroutine, Dict, List, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.metadata_extraction.sql import (
    BaseSQLMetadataExtractionActivities,
)
from application_sdk.constants import APPLICATION_NAME
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.workflows.metadata_extraction import MetadataExtractionWorkflow

logger = get_logger(__name__)
workflow.logger = logger


@workflow.defn
class BaseSQLMetadataExtractionWorkflow(MetadataExtractionWorkflow):
    """Workflow for extracting metadata from SQL databases.

    This workflow orchestrates the extraction of metadata from SQL databases, including
    databases, schemas, tables, and columns. It handles the fetching and transformation
    of metadata in batches for efficient processing.

    Attributes:
        activities_cls (Type[BaseSQLMetadataExtractionActivities]): The activities class
            containing the implementation of metadata extraction operations.
        application_name (str): Name of the application, set to "sql-connector".
    """

    activities_cls: Type[BaseSQLMetadataExtractionActivities] = (
        BaseSQLMetadataExtractionActivities
    )

    application_name: str = APPLICATION_NAME

    @staticmethod
    def get_activities(
        activities: BaseSQLMetadataExtractionActivities,
    ) -> Sequence[Callable[..., Any]]:
        """Get the sequence of activities to be executed by the workflow.

        Args:
            activities (ActivitiesInterface): The activities instance
                containing the metadata extraction operations.

        Returns:
            Sequence[Callable[..., Any]]: A sequence of activity methods to be executed
                in order, including preflight check, fetching databases, schemas,
                tables, columns, and transforming data.
        """
        return [
            activities.preflight_check,
            activities.get_workflow_args,
            activities.fetch_databases,
            activities.fetch_schemas,
            activities.fetch_tables,
            activities.fetch_columns,
            activities.fetch_procedures,
            activities.transform_data,
        ]

    async def fetch_and_transform(
        self,
        fetch_fn: Callable[
            [Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any] | None]
        ],
        workflow_args: Dict[str, Any],
        retry_policy: RetryPolicy,
    ) -> None:
        """Fetch and transform metadata using the provided fetch function.

        This method executes a fetch operation and transforms the resulting data. It handles
        chunking of data and parallel processing of transformations.

        Args:
            fetch_fn (Callable): The function to fetch metadata.
            workflow_args (Dict[str, Any]): Arguments for the workflow execution.
            retry_policy (RetryPolicy): The retry policy for activity execution.

        Raises:
            ValueError: If chunk_count, raw_total_record_count, or typename is invalid.
        """
        raw_statistics = await workflow.execute_activity_method(
            fetch_fn,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )
        if raw_statistics is None:
            return

        activity_statistics = ActivityStatistics.model_validate(raw_statistics)
        transform_activities: List[Any] = []

        if activity_statistics is None or activity_statistics.chunk_count == 0:
            # to handle the case where the fetch_fn returns None or no chunks
            return

        if activity_statistics.typename is None:
            raise ValueError("Invalid typename")

        batches, chunk_starts = self.get_transform_batches(
            activity_statistics.chunk_count, activity_statistics.typename
        )

        for i in range(len(batches)):
            transform_activities.append(
                workflow.execute_activity_method(
                    self.activities_cls.transform_data,
                    {
                        "typename": activity_statistics.typename,
                        "file_names": batches[i],
                        "chunk_start": chunk_starts[i],
                        **workflow_args,
                    },
                    retry_policy=retry_policy,
                    start_to_close_timeout=self.default_start_to_close_timeout,
                    heartbeat_timeout=self.default_heartbeat_timeout,
                )
            )

        record_counts = await asyncio.gather(*transform_activities)

        # Calculate the parameters necessary for writing metadata
        total_record_count = 0
        chunk_count = 0
        for record_count in record_counts:
            metadata_model = ActivityStatistics.model_validate(record_count)
            total_record_count += metadata_model.total_record_count
            chunk_count += metadata_model.chunk_count

    def get_transform_batches(self, chunk_count: int, typename: str):
        """Get batches for parallel transformation processing.

        Args:
            chunk_count (int): Total number of chunks to process.
            typename (str): Type name for the chunks.

        Returns:
            Tuple[List[List[str]], List[int]]: A tuple containing:
                - List of batches, where each batch is a list of file paths
                - List of starting chunk numbers for each batch
        """
        batches: List[List[str]] = []
        chunk_start_numbers: List[int] = []

        for i in range(chunk_count):
            # Track starting chunk number (which is just i)
            chunk_start_numbers.append(i)

            # Each batch contains exactly one chunk
            batches.append([f"{typename}/{i+1}.json"])

        return batches, chunk_start_numbers

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        """Run the SQL metadata extraction workflow.

        This method orchestrates the entire metadata extraction process, including:
        1. Setting up workflow configuration
        2. Executing preflight checks
        3. Fetching and transforming databases, schemas, tables, and columns
        4. Writing metadata to storage

        Args:
            workflow_config (Dict[str, Any]): Includes workflow_id and other parameters
                workflow_id is used to extract the workflow configuration from the
                state store.

        Note:
            The workflow uses a retry policy with maximum 6 attempts and backoff
            coefficient of 2.
            In case you override the run method, annotate it with @workflow.run
        """
        start_time = time()
        workflow_id = workflow_config["workflow_id"]
        workflow_success = False

        try:
            await super().run(workflow_config)

            # Get the workflow configuration from the state store
            workflow_args: Dict[str, Any] = await workflow.execute_activity_method(
                self.activities_cls.get_workflow_args,
                workflow_config,  # Pass the whole config containing workflow_id
                retry_policy=RetryPolicy(maximum_attempts=3, backoff_coefficient=2),
                start_to_close_timeout=self.default_start_to_close_timeout,
                heartbeat_timeout=self.default_heartbeat_timeout,
            )

            workflow_run_id = workflow.info().run_id
            workflow_args["workflow_run_id"] = workflow_run_id

            logger.info(f"Starting extraction workflow for {workflow_id}")
            retry_policy = RetryPolicy(
                maximum_attempts=6,
                backoff_coefficient=2,
            )

            output_prefix = workflow_args["output_prefix"]
            output_path = f"{output_prefix}/{workflow_id}/{workflow_run_id}"
            workflow_args["output_path"] = output_path

            fetch_functions = self.get_fetch_functions()

            fetch_and_transforms = [
                self.fetch_and_transform(fetch_function, workflow_args, retry_policy)
                for fetch_function in fetch_functions
            ]

            await asyncio.gather(*fetch_and_transforms)

            logger.info(f"Extraction workflow completed for {workflow_id}")
            workflow_success = True
        except Exception as e:
            logger.error(f"Workflow failed for {workflow_id}: {str(e)}")
            workflow_success = False
            raise
        finally:
            # Record workflow execution time metric
            execution_time = time() - start_time
            metrics = get_metrics()
            metrics.record_metric(
                name="workflow_execution_time_seconds",
                value=execution_time,
                metric_type=MetricType.GAUGE,
                labels={
                    "workflow_id": workflow_id,
                    "workflow_type": "sql_metadata_extraction",
                    "status": "success" if workflow_success else "error",
                },
                description="Total execution time of SQL metadata extraction workflow in seconds",
                unit="s",
            )

    def get_fetch_functions(
        self,
    ) -> list[
        Callable[
            [BaseSQLMetadataExtractionActivities, Dict[str, Any]],
            Coroutine[Any, Any, Any],
        ]
    ]:
        """Get the list of functions for fetching SQL metadata.

        This method returns a sequence of coroutine functions that fetch different
        types of SQL metadata. The functions are executed in order to extract
        metadata about databases, schemas, tables, columns, and procedures.

        Each fetch function takes a dictionary of arguments and returns a coroutine
        that resolves to either a dictionary of metadata or None if no metadata
        is available.

        Returns:
            List[Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any] | None]]]:
                A list of fetch functions in the order they should be executed:
                1. fetch_databases: Fetch database metadata
                2. fetch_schemas: Fetch schema metadata
                3. fetch_tables: Fetch table metadata
                4. fetch_columns: Fetch column metadata
                5. fetch_procedures: Fetch stored procedure metadata
        """
        return [
            self.activities_cls.fetch_databases,
            self.activities_cls.fetch_schemas,
            self.activities_cls.fetch_tables,
            self.activities_cls.fetch_columns,
            self.activities_cls.fetch_procedures,
        ]
