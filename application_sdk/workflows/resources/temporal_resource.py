"""TODO: Module docstring"""

import logging
import os
import uuid
from abc import ABC
from typing import Any, Dict, Sequence

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


class ResourceInterface(ABC):
    def __init__(self):
        pass

    async def load(self):
        pass

    def set_credentials(self, credentials: Dict[str, Any]):
        pass


class TemporalConfig:
    host = os.getenv("host", "localhost")
    port = os.getenv("port", "7233")
    application_name = os.getenv("application_name", "default")
    # FIXME: causes issue with different namespace, TBR.
    namespace: str = "default"

    def __init__(
        self,
        host: str | None = None,
        port: str | None = None,
        application_name: str | None = None,
        namespace: str | None = "default",
    ):
        if host:
            self.host = host

        if port:
            self.port = port

        if application_name:
            self.application_name = application_name

        if namespace:
            self.namespace = namespace

    def get_worker_task_queue(self) -> str:
        return f"{self.application_name}"

    def get_connection_string(self) -> str:
        return f"{self.host}:{self.port}"

    def get_namespace(self) -> str:
        return self.namespace


class TemporalResource(ResourceInterface):
    workflow_class = ClassType
    activities: Sequence[CallableType] = []
    passthrough_modules: Sequence[str] = ["application_sdk", "os"]

    def __init__(
        self,
        temporal_config: TemporalConfig,
    ):
        self.config = temporal_config
        self.client = None
        self.worker = None
        self.worker_task_queue = self.config.get_worker_task_queue()

        workflow.logger = AtlanLoggerAdapter(logging.getLogger(__name__))
        activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))

        super().__init__()

    async def load(self):
        self.client = await Client.connect(
            self.config.get_connection_string(),
            namespace=self.config.get_namespace(),
        )

    async def start_workflow(
        self, workflow_args: Any, workflow_class: Any, workflow_id: str | None = None
    ):
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
            handle = await self.client.start_workflow(
                workflow_class,
                workflow_args,
                id=workflow_id,
                task_queue=self.worker_task_queue,
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

    def create_worker(
        self, activities: Sequence[CallableType], workflow_class: Any
    ) -> Worker:
        if not self.client:
            raise ValueError("Client is not loaded")

        return Worker(
            self.client,
            task_queue=self.worker_task_queue,
            workflows=[workflow_class],
            activities=activities,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    *self.passthrough_modules
                )
            ),
        )
