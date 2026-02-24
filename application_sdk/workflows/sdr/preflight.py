"""PreflightCheckWorkflow -- SDR workflow for connector preflight checks."""

from datetime import timedelta
from typing import Any, Callable, Dict, Sequence, Type

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities.sdr.preflight import PreflightCheckActivities
from application_sdk.workflows import WorkflowInterface


@workflow.defn
class PreflightCheckWorkflow(WorkflowInterface[PreflightCheckActivities]):
    """Generic workflow that performs preflight checks for a connector.

    Calls get_workflow_args to retrieve the full config from StateStore, then
    calls the preflight activity which delegates to handler.preflight_check().

    This workflow is automatically registered by BaseApplication.setup_workflow()
    and requires no app-level configuration.
    """

    activities_cls: Type[PreflightCheckActivities] = PreflightCheckActivities

    default_start_to_close_timeout: timedelta = timedelta(minutes=10)
    default_heartbeat_timeout: timedelta = timedelta(minutes=2)

    @staticmethod
    def get_activities(
        activities: PreflightCheckActivities,
    ) -> Sequence[Callable[..., Any]]:
        return [
            activities.preflight,
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        retry_policy = RetryPolicy(maximum_attempts=3, backoff_coefficient=2)

        workflow_args = await workflow.execute_activity_method(
            self.activities_cls.get_workflow_args,
            workflow_config,
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        result: Dict[str, Any] = await workflow.execute_activity_method(
            self.activities_cls.preflight,
            args=[workflow_args],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

        workflow.logger.info("PreflightCheckWorkflow completed")
        return result
