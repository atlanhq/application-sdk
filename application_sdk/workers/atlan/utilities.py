from typing import Any, Dict

from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.worker import Worker
from application_sdk.workflows.atlan.utilities import UtilitiesAtlanWorkflow

logger = get_logger(__name__)


class UtilitiesAtlanWorker(Worker):
    """Worker for managing Atlan utilities workflows.

    Note: Make sure a single worker is created per pod as it internally creates
    multiple threads to execute activities.
    """

    TASK_QUEUE = "atlan-utilities"

    def __init__(
        self, client: TemporalClient, *args: Dict[str, Any], **kwargs: Dict[str, Any]
    ):
        super().__init__(
            client,
            workflow_classes=[UtilitiesAtlanWorkflow],
            temporal_activities=UtilitiesAtlanWorkflow.get_activities(),
            *args,
            **kwargs,
        )
