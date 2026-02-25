"""FetchMetadataWorkflow -- SDR workflow for fetching connector filter metadata."""

from datetime import timedelta
from typing import Any, Callable, Dict, List, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.sdr.fetch_metadata import FetchMetadataActivities
from application_sdk.workflows import WorkflowInterface


@workflow.defn
class FetchMetadataWorkflow(WorkflowInterface[FetchMetadataActivities]):
    """Generic workflow that fetches filter metadata (component/schema hierarchy).

    Calls get_workflow_args to retrieve the full config from StateStore, then
    calls the fetch_metadata activity which delegates to handler.fetch_metadata().

    This workflow is automatically registered by BaseApplication.setup_workflow()
    and requires no app-level configuration.
    """

    activities_cls: Type[FetchMetadataActivities] = FetchMetadataActivities

    default_start_to_close_timeout: timedelta = timedelta(minutes=10)
    default_heartbeat_timeout: timedelta = timedelta(minutes=2)

    @staticmethod
    def get_activities(
        activities: FetchMetadataActivities,
    ) -> Sequence[Callable[..., Any]]:
        return [
            activities.get_workflow_args,
            activities.fetch_metadata,
        ]

    @workflow.run
    async def run(  # type: ignore[override]
        self, workflow_config: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        retry_policy = RetryPolicy(maximum_attempts=3, backoff_coefficient=2)

        workflow_args = await workflow.execute_activity_method(
            self.activities_cls.get_workflow_args,
            workflow_config,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        result: List[Dict[str, Any]] = await workflow.execute_activity_method(
            self.activities_cls.fetch_metadata,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        workflow.logger.info(
            "FetchMetadataWorkflow completed with %d items", len(result)
        )
        return result
