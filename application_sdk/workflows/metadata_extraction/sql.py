"""SQL metadata extraction workflow implementation.

This module provides the workflow implementation for extracting metadata from SQL databases,
including databases, schemas, tables, and columns.
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any, Callable, Coroutine, Dict, List, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.activities.metadata_extraction.sql import (
    SQLMetadataExtractionActivities,
)
from application_sdk.common.constants import ApplicationConstants
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.workflows.metadata_extraction import MetadataExtractionWorkflow

workflow.logger = get_logger()


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

    application_name: str = ApplicationConstants.APPLICATION_NAME.value

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
            activities.write_type_metadata,
            activities.write_raw_type_metadata,
        ]

    async def fetch_and_transform(
        self,
        fetch_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, Dict[str, Any]]],
        workflow_args: Dict[str, Any],
        retry_policy: RetryPolicy,
        start_to_close_timeout_seconds: int = 1000,
    ) -> None:
        """Fetch and transform metadata using the provided fetch function.

        This method executes a fetch operation and transforms the resulting data. It handles
        chunking of data and parallel processing of transformations.

        Args:
            fetch_fn (Callable): The function to fetch metadata.
            workflow_args (Dict[str, Any]): Arguments for the workflow execution.
            retry_policy (RetryPolicy): The retry policy for activity execution.
            start_to_close_timeout_seconds (int): The start to close timeout for the
                activity execution.

        Raises:
            ValueError: If chunk_count, raw_total_record_count, or typename is invalid.
        """
        raw_stat = await workflow.execute_activity_method(
            fetch_fn,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=start_to_close_timeout_seconds),
            heartbeat_timeout=timedelta(seconds=120),
        )
        raw_stat = ActivityStatistics.model_validate(raw_stat)
        transform_activities: List[Any] = []

        if raw_stat is None or raw_stat.chunk_count == 0:
            # to handle the case where the fetch_fn returns None or no chunks
            return

        if raw_stat.typename is None:
            raise ValueError("Invalid typename")

        # Write the raw metadata
        await workflow.execute_activity_method(
            self.activities_cls.write_raw_type_metadata,
            {
                "total_record_count": raw_stat.total_record_count,
                "chunk_count": raw_stat.chunk_count,
                "typename": raw_stat.typename,
                **workflow_args,
            },
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=start_to_close_timeout_seconds),
            heartbeat_timeout=timedelta(seconds=120),
        )

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
                    start_to_close_timeout=timedelta(
                        seconds=start_to_close_timeout_seconds
                    ),
                    heartbeat_timeout=timedelta(seconds=120),
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

        # Write the transformed metadata
        await workflow.execute_activity_method(
            self.activities_cls.write_type_metadata,
            {
                "total_record_count": total_record_count,
                "chunk_count": chunk_count,
                "typename": raw_stat.typename,
                **workflow_args,
            },
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=start_to_close_timeout_seconds),
            heartbeat_timeout=timedelta(seconds=120),
        )

    def get_transform_batches(self, chunk_count: int, typename: str):
        """Get batches for parallel transformation processing.

        This method divides the total chunks into batches for parallel processing,
        considering the maximum concurrency level.

        Args:
            chunk_count (int): Total number of chunks to process.
            typename (str): Type name for the chunks.

        Returns:
            Tuple[List[List[str]], List[int]]: A tuple containing:
                - List of batches, where each batch is a list of file paths
                - List of starting chunk numbers for each batch
        """
        # concurrency logic
        concurrency_level = chunk_count

        batches: List[List[str]] = []
        chunk_start_numbers: List[int] = []
        start = 0
        for i in range(concurrency_level):
            current_batch_start = start
            chunk_start_numbers.append(current_batch_start)
            current_batch_count = int(chunk_count / concurrency_level)
            if i < chunk_count % concurrency_level and chunk_count > concurrency_level:
                current_batch_count += 1

            batches.append(
                [
                    f"{typename}/{i}.json"
                    for i in range(
                        current_batch_start + 1,
                        current_batch_start + current_batch_count + 1,
                    )
                ]
            )
            start += current_batch_count

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
            workflow_config (Dict[str, Any]): Configuration for the workflow execution,
                including workflow_id and other parameters.

        Note:
            The workflow uses a retry policy with maximum 6 attempts and backoff
            coefficient of 2.
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

        fetch_and_transforms = [
            self.fetch_and_transform(
                self.activities_cls.fetch_databases,
                workflow_args,
                retry_policy,
            ),
            self.fetch_and_transform(
                self.activities_cls.fetch_schemas,
                workflow_args,
                retry_policy,
            ),
            self.fetch_and_transform(
                self.activities_cls.fetch_tables,
                workflow_args,
                retry_policy,
            ),
            self.fetch_and_transform(
                self.activities_cls.fetch_columns,
                workflow_args,
                retry_policy,
            ),
        ]

        await asyncio.gather(*fetch_and_transforms)

        workflow.logger.info(f"Extraction workflow completed for {workflow_id}")
