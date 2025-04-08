from datetime import timedelta
from typing import Any, Callable, Dict, Optional, Sequence

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.atlan.diff import DiffAtlanActivities
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


@workflow.defn
class UtilitiesAtlanWorkflow(WorkflowInterface):
    @staticmethod
    def get_activities(
        activities: Optional[ActivitiesInterface] = None,
    ) -> Sequence[Callable[..., Any]]:
        activities = DiffAtlanActivities()
        return [activities.calculate_diff]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        retry_policy = RetryPolicy(
            maximum_attempts=2,
            backoff_coefficient=2,
        )

        workflow.logger.info("Running workflow")

        await workflow.execute_activity_method(
            self.get_activities()[0],
            retry_policy=retry_policy,
            start_to_close_timeout=timedelta(seconds=2 * 60 * 60),
        )
