from typing import Type
from application_sdk.activities.metadata_extraction.sql import SQLExtractionActivities
from temporalio import workflow
from application_sdk.workflows import WorkflowInterface
from temporalio.common import RetryPolicy
import asyncio
from datetime import timedelta
from typing import Dict, Any, Sequence, Callable
from application_sdk.inputs.statestore import StateStore

@workflow.defn
class SQLMetadataExtractionWorkflow(WorkflowInterface):
    activities_cls: Type[SQLExtractionActivities] = SQLExtractionActivities

    def __init__(self, activities_cls: Type[SQLExtractionActivities] = SQLExtractionActivities):
        super().__init__(activities_cls=activities_cls)

    def get_activities(self) -> Sequence[Callable[..., Any]]:
        return [
            self.activities_cls.fetch_databases,
            self.activities_cls.fetch_tables,
            self.activities_cls.fetch_columns,
            self.activities_cls.transform_metadata,
            self.activities_cls.write_metadata,
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )

        workflow_args = StateStore.extract_configuration(workflow_config["workflow_id"])

        await workflow.execute_activity_method(
            self.activities_cls.fetch_databases,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=1000),
        )

        await asyncio.gather(
            workflow.execute_activity_method(
                self.activities_cls.fetch_tables,
                args=[workflow_args],
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            ),
            workflow.execute_activity_method(
                self.activities_cls.fetch_columns,
                args=[workflow_args],
                retry_policy=retry_policy,
                start_to_close_timeout=timedelta(seconds=1000),
            ),
        )

        
