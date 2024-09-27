import logging
import os
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence

from temporalio import workflow, activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.types import CallableType, ClassType
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from application_sdk.logging import get_logger

logger = get_logger(__name__)


class WorkflowAuthInterface(ABC):
    @abstractmethod
    def test_auth(self, credential: Dict[str, Any]) -> bool:
        raise NotImplementedError


class WorkflowMetadataInterface(ABC):

    @abstractmethod
    def fetch_metadata(self, credential: Dict[str, Any]) -> List[Dict[str, str]]:
        raise NotImplementedError


class WorkflowPreflightCheckInterface(ABC):

    @abstractmethod
    def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class WorkflowWorkerInterface(ABC):
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

    @abstractmethod
    async def run(self, *args, **kwargs):
        raise NotImplementedError

    async def run_workflow(self, workflow_args: Any) -> Dict[str, Any]:
        client = await Client.connect(
            f"{self.TEMPORAL_HOST}:{self.TEMPORAL_PORT}",
            namespace="default"
            # FIXME: causes issue with different namespace, TBR.
        )

        workflow_id = str(uuid.uuid4())
        workflow.logger.setLevel(logging.DEBUG)
        activity.logger.setLevel(logging.DEBUG)

        try:
            handle = await client.start_workflow(
                self.TEMPORAL_WORKFLOW_CLASS,
                workflow_args,
                id=workflow_id,
                task_queue=self.TEMPORAL_WORKER_TASK_QUEUE,
            )
            logger.info(f"Workflow started: {handle.id} {handle.result_run_id}")
            return {
                "message": "Workflow started",
                "workflow_id": handle.id,
                "run_id": handle.result_run_id,
            }
        except WorkflowFailureError as e:
            logger.error(f"Workflow failure: {e}")
            raise e

    async def start_worker(self):
        self.temporal_client = await Client.connect(
            f"{self.TEMPORAL_HOST}:{self.TEMPORAL_PORT}",
            namespace="default"
            # FIXME: causes issue with namespace other than default, To be reviewed.
        )

        self.temporal_worker = Worker(
            self.temporal_client,
            task_queue=self.TEMPORAL_WORKER_TASK_QUEUE,
            workflows=[self.TEMPORAL_WORKFLOW_CLASS],
            activities=self.TEMPORAL_ACTIVITIES,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    *self.PASSTHROUGH_MODULES
                )
            )
        )

        await self.temporal_worker.run()


class WorkflowBuilderInterface(ABC):
    def __init__(
        self,
        auth_interface: Optional[WorkflowAuthInterface] = None,
        metadata_interface: Optional[WorkflowMetadataInterface] = None,
        preflight_check_interface: Optional[WorkflowPreflightCheckInterface] = None,
        worker_interface: Optional[WorkflowWorkerInterface] = None,
    ):
        self.auth_interface = auth_interface
        self.metadata_interface = metadata_interface
        self.preflight_check_interface = preflight_check_interface
        self.worker_interface = worker_interface
