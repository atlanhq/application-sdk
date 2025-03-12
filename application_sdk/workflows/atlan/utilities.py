from typing import Sequence, Callable, Any, Dict, Optional

from temporalio import workflow

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.atlan.diff import DiffAtlanActivities
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.workflows import WorkflowInterface


logger = get_logger(__name__)


@workflow.defn
class UtilitiesAtlanWorkflow(WorkflowInterface):
    @staticmethod
    def get_activities(activities: Optional[ActivitiesInterface]=None) -> Sequence[Callable[..., Any]]:
        activities = DiffAtlanActivities()
        return [
            activities.calculate_diff_test
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        raise NotImplementedError("Workflow run method not implemented")
