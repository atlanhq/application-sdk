from typing import Any, Callable, Optional, Sequence

from application_sdk.activities import ActivitiesInterface
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.publish_app.activities.atlas import AtlasPublishAtlanActivities
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class AtlasPublishAtlanWorkflow(WorkflowInterface):
    """Workflow for Atlas publish operations."""

    @staticmethod
    def get_activities(
        activities: Optional[ActivitiesInterface] = None,
    ) -> Sequence[Callable[..., Any]]:
        """Get activities for the workflow.

        Args:
            activities: Activities interface (optional)

        Returns:
            List of activities
        """
        activities = AtlasPublishAtlanActivities()
        return [
            activities.setup_connection_entity,
            activities.plan,
            activities.publish,
            activities.delete,
        ]
