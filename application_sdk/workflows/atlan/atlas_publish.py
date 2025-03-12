from typing import Callable, Any, Dict, Sequence, Optional

from application_sdk.activities import ActivitiesInterface
from application_sdk.activities.atlan.atlas_publish import AtlasPublishAtlanActivities
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.workflows import WorkflowInterface
from temporalio import workflow

logger = get_logger(__name__)


@workflow.defn
class AtlasPublishAtlanWorkflow(WorkflowInterface):
    @staticmethod
    def get_activities(activities: Optional[ActivitiesInterface]=None) -> Sequence[Callable[..., Any]]:
        activities = AtlasPublishAtlanActivities()
        return [
            activities.publish
        ]

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        raise NotImplementedError("Workflow run method not implemented")