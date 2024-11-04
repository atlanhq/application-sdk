"""TODO: Module docstring"""
import logging
import os
import uuid
from abc import ABC
from typing import Sequence

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.types import CallableType, ClassType
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.logging import get_logger

logger = get_logger(__name__)


class Resource(ABC):
    def __init__(self):
        pass

    async def load(self):
        pass

class TemporalResource(Resource):

    TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost")
    TEMPORAL_PORT = os.getenv("TEMPORAL_PORT", "7233")

    TEMPORAL_WORKFLOW_CLASS = ClassType
    TEMPORAL_ACTIVITIES: Sequence[CallableType] = []
    PASSTHROUGH_MODULES: Sequence[str] = ["application_sdk"]

    def __init__(self, application_name: str):
        self.temporal_client = None
        self.temporal_worker = None
        self.application_name = application_name
        self.TEMPORAL_WORKER_TASK_QUEUE = f"{self.application_name}"

        workflow.logger = AtlanLoggerAdapter(logging.getLogger(__name__))
        activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))

        super().__init__()

    async def load(self):
        self.temporal_client = await Client.connect(
            f"{self.TEMPORAL_HOST}:{self.TEMPORAL_PORT}",
            namespace="default",
            # FIXME: causes issue with different namespace, TBR.
        )

    def set_activities(self, activities: Sequence[CallableType]):
        self.TEMPORAL_ACTIVITIES = activities

    async def start_workflow(self, workflow_args, workflow_id: str = None):
        workflow_id = workflow_id or str(uuid.uuid4())
        workflow_args.update(
            {
                "workflow_id": workflow_id,
                "output_prefix": "/tmp/output",
            }
        )

        workflow.logger.setLevel(logging.DEBUG)
        activity.logger.setLevel(logging.DEBUG)


        try:
            handle = await self.temporal_client.start_workflow(
                self.TEMPORAL_WORKFLOW_CLASS,
                workflow_args,
                id=workflow_id,
                task_queue=self.TEMPORAL_WORKER_TASK_QUEUE,
            )
            workflow.logger.info(
                f"Workflow started: {handle.id} {handle.result_run_id}"
            )
            return {
                "workflow_id": handle.id,
                "run_id": handle.result_run_id,
            }
        except WorkflowFailureError as e:
            logger.error(f"Workflow failure: {e}")
            raise e

        return {}

    def create_worker(self):
        return Worker(
            self.temporal_client,
            task_queue=self.TEMPORAL_WORKER_TASK_QUEUE,
            workflows=[self.TEMPORAL_WORKFLOW_CLASS],
            activities=self.TEMPORAL_ACTIVITIES,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    *self.PASSTHROUGH_MODULES
                )
            ),
        )