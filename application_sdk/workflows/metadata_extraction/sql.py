import asyncio
from datetime import timedelta
from typing import Any, Callable, Coroutine, Dict, List, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.metadata_extraction.sql import (
    SQLMetadataExtractionActivities,
)
from application_sdk.common.constants import ApplicationConstants
from application_sdk.inputs.statestore import StateStore
from application_sdk.workflows.metadata_extraction import MetadataExtractionWorkflow


@workflow.defn
class SQLMetadataExtractionWorkflow(MetadataExtractionWorkflow):
    activities_cls: Type[SQLMetadataExtractionActivities] = (
        SQLMetadataExtractionActivities
    )

    application_name: str = ApplicationConstants.APPLICATION_NAME.value
    batch_size: int = 100000
    max_transform_concurrency: int = 5

    @staticmethod
    def get_activities(
        activities: SQLMetadataExtractionActivities,
    ) -> Sequence[Callable[..., Any]]:
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
    ) -> None:
        raw_stat = await workflow.execute_activity_method(
            fetch_fn,
            workflow_args,
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )
        transform_activities: List[Any] = []

        if raw_stat is None or len(raw_stat) == 0:
            # to handle the case where the fetch_fn returns None or []
            return

        chunk_count = max(value.get("chunk_count", 0) for value in raw_stat)
        if chunk_count is None:
            raise ValueError("Invalid chunk_count")

        raw_total_record_count = max(
            value.get("total_record_count", 0) for value in raw_stat
        )
        if raw_total_record_count is None:
            raise ValueError("Invalid raw_total_record_count")

        if chunk_count == 0:
            return

        typename = raw_stat[0].get("typename")
        if typename is None:
            raise ValueError("Invalid typename")

        # Write the raw metadata
        await workflow.execute_activity_method(
            self.activities_cls.write_raw_type_metadata,
            {
                "record_count": raw_total_record_count,
                "chunk_count": chunk_count,
                "typename": typename,
                **workflow_args,
            },
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        batches, chunk_starts = self.get_transform_batches(chunk_count, typename)

        for i in range(len(batches)):
            transform_activities.append(
                workflow.execute_activity_method(
                    self.activities_cls.transform_data,
                    {
                        "typename": typename,
                        "batch": batches[i],
                        "chunk_start": chunk_starts[i],
                        **workflow_args,
                    },
                    retry_policy=retry_policy,
                    start_to_close_timeout=timedelta(seconds=1000),
                )
            )

        record_counts = await asyncio.gather(*transform_activities)

        # Calculate the parameters necessary for writing metadata
        total_record_count = sum(
            max(
                record_output.get("total_record_count", 0)
                for record_output in record_count
            )
            for record_count in record_counts
        )
        chunk_count = sum(
            max(record_output.get("chunk_count", 0) for record_output in record_count)
            for record_count in record_counts
        )

        # Write the transformed metadata
        await workflow.execute_activity_method(
            self.activities_cls.write_type_metadata,
            {
                "record_count": total_record_count,
                "chunk_count": chunk_count,
                "typename": typename,
                **workflow_args,
            },
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

    def get_transform_batches(self, chunk_count: int, typename: str):
        # concurrency logic
        concurrency_level = min(
            self.max_transform_concurrency,
            chunk_count,
        )

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
        await super().run(workflow_config)

        workflow_id = workflow_config["workflow_id"]
        workflow_args: Dict[str, Any] = StateStore.extract_configuration(workflow_id)

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
