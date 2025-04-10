from application_sdk.clients.workflow import WorkflowClient
from application_sdk.worker import Worker
from application_sdk.utilities_app.workflows.publish.atlas import AtlasPublishAtlanWorkflow
from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)


class AtlasPublishAtlanWorker(Worker):
    """Worker for managing Atlan Atlas Publish workflows.
    """

    # FIXME: this should be an input via environment variable
    TASK_QUEUE = "atlan_atlas_publish"

    def __init__(self, client: WorkflowClient, *args, **kwargs):
        super().__init__(
            client,
            workflow_activities=AtlasPublishAtlanWorkflow.get_activities(),
            is_sync_activities=False,
            *args,
            **kwargs
        )