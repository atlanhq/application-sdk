from application_sdk.activities.atlan.atlas_publish import AtlasPublishAtlanActivities
from application_sdk.clients.temporal import TemporalClient
from application_sdk.worker import Worker
from application_sdk.workflows.atlan.atlas_publish import AtlasPublishAtlanWorkflow
from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)


class AtlasPublishAtlanWorker(Worker):
    """Worker for managing Atlan Atlas Publish workflows.
    """
    TASK_QUEUE = "atlan_atlas_publish"

    def __init__(self, client: TemporalClient, *args, **kwargs):
        super().__init__(
            client,
            temporal_activities=AtlasPublishAtlanWorkflow.get_activities(),
            is_sync_activities=False,
            *args,
            **kwargs
        )
