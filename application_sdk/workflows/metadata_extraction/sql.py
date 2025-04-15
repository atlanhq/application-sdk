"""SQL metadata extraction workflow implementation.

This module provides the workflow implementation for extracting metadata from SQL databases,
including databases, schemas, tables, and columns.
"""

import asyncio
from typing import Any, Callable, Coroutine, Dict, List, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.metadata_extraction.sql import (
    SQLMetadataExtractionActivities,
)
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.workflows.metadata_extraction import MetadataExtractionWorkflow
from application_sdk.constants import (
    APPLICATION_NAME,
)

workflow.logger = get_logger(__name__)


@workflow.defn
class SQLMetadataExtractionWorkflow(MetadataExtractionWorkflow):
    """Workflow for extracting metadata from SQL databases.

    This workflow orchestrates the extraction of metadata from SQL databases, including
    databases, schemas, tables, and columns. It handles the fetching and transformation
    of metadata in batches for efficient processing.

    Attributes:
        activities_cls (Type[SQLMetadataExtractionActivities]): The activities class
            containing the implementation of metadata extraction operations.
        application_name (str): Name of the application, set to "sql-connector".
    """

    activities_cls: Type[SQLMetadataExtractionActivities] = (
        SQLMetadataExtractionActivities
    )

    application_name: str = APPLICATION_NAME

    @staticmethod
    def get_activities(
        activities: SQLMetadataExtractionActivities,
    ) -> Sequence[Callable[..., Any]]:
        """Get the sequence of activities to be executed by the workflow.

        Args:
            activities (SQLMetadataExtractionActivities): The activities instance
                containing the metadata extraction operations.

        Returns:
            Sequence[Callable[..., Any]]: A sequence of activity methods to be executed
                in order, including preflight check, fetching databases, schemas,
                tables, columns, and transforming data.
        """
        return [
            activities.preflight_check,
            activities.fetch_databases,
            activities.fetch_schemas,
            activities.fetch_tables,
            activities.fetch_columns,
            activities.transform_data,
        ]

    async def fetch_and_transform(
        self,
        fetch_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]],
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
        raw_stat = await workflow.execute_activity_method(
            fetch_fn,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )
        raw_stat = ActivityStatistics.model_validate(raw_stat)
        transform_activities: List[Any] = []

        if raw_stat is None or raw_stat.chunk_count == 0:
            # to handle the case where the fetch_fn returns None or no chunks
            return

        if raw_stat.typename is None:
            raise ValueError("Invalid typename")

        batches, chunk_starts = self.get_transform_batches(
            raw_stat.chunk_count, raw_stat.typename
        )

        for i in range(len(batches)):
            transform_activities.append(
                workflow.execute_activity_method(
                    self.activities_cls.transform_data,
                    {
                        "typename": raw_stat.typename,
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
        await super().run(workflow_config)

        workflow_id = workflow_config["workflow_id"]
        workflow_args: Dict[str, Any] = StateStoreInput.extract_configuration(
            workflow_id
        )

        workflow_run_id = workflow.info().run_id
        workflow_args["workflow_run_id"] = workflow_run_id

        workflow.logger.info(f"Starting extraction workflow for {workflow_id}")
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

        workflow.logger.info(f"Extraction workflow completed for {workflow_id}")


    def get_fetch_functions(self) -> List[Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]]:
        """Get the fetch functions for the SQL metadata extraction workflow.

        Returns:
            List[Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]]]: A list of fetch operations.
        """
        return [
            self.activities_cls.fetch_databases,
            self.activities_cls.fetch_schemas,
            self.activities_cls.fetch_tables,
            self.activities_cls.fetch_columns,
        ]
