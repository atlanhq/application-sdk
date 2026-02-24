"""TestAuthWorkflow -- SDR workflow for testing connector authentication."""

from datetime import timedelta
from typing import Any, Callable, Dict, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.sdr.test_auth import TestAuthActivities
from application_sdk.workflows import WorkflowInterface


@workflow.defn
class TestAuthWorkflow(WorkflowInterface[TestAuthActivities]):
    """Generic workflow that tests connector authentication credentials.

    Calls get_workflow_args to retrieve the full config from StateStore, then
    calls the test_auth activity which delegates to handler.test_auth().

    This workflow is automatically registered by BaseApplication.setup_workflow()
    and requires no app-level configuration.
    """

    activities_cls: Type[TestAuthActivities] = TestAuthActivities

    default_start_to_close_timeout: timedelta = timedelta(minutes=5)
    default_heartbeat_timeout: timedelta = timedelta(minutes=2)

    @staticmethod
    def get_activities(
        activities: TestAuthActivities,
    ) -> Sequence[Callable[..., Any]]:
        return [
            activities.test_auth,
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> bool:  # type: ignore[override]
        retry_policy = RetryPolicy(maximum_attempts=3, backoff_coefficient=2)

        workflow_args = await workflow.execute_activity_method(
            self.activities_cls.get_workflow_args,
            workflow_config,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        result: bool = await workflow.execute_activity_method(
            self.activities_cls.test_auth,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        workflow.logger.info("TestAuthWorkflow completed: %s", result)
        return result
