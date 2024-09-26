import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Dict, List, Optional, Sequence

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.types import CallableType, ClassType
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from application_sdk.dto.workflow import WorkflowConfig, WorkflowRequestPayload
from application_sdk.interfaces.platform import Platform
from application_sdk.logging import get_logger

logger = get_logger(__name__)


class WorkflowAuthInterface(ABC):
    def __init__(
        self,
        get_sql_alchemy_string_fn: Callable[[Dict[str, Any]], str],
        get_sql_alchemy_connect_args_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ):
        self.get_sql_alchemy_string_fn = get_sql_alchemy_string_fn
        self.get_sql_alchemy_connect_args_fn = get_sql_alchemy_connect_args_fn

    @abstractmethod
    def test_auth(self, credential: Dict[str, Any]) -> bool:
        raise NotImplementedError


class WorkflowMetadataInterface(ABC):
    def __init__(
        self,
        get_sql_alchemy_string_fn: Callable[[Dict[str, Any]], str],
        get_sql_alchemy_connect_args_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ):
        self.get_sql_alchemy_string_fn = get_sql_alchemy_string_fn
        self.get_sql_alchemy_connect_args_fn = get_sql_alchemy_connect_args_fn

    @abstractmethod
    def fetch_metadata(self, credential: Dict[str, Any]) -> List[Dict[str, str]]:
        raise NotImplementedError


class WorkflowPreflightCheckInterface(ABC):
    def __init__(
        self,
        get_sql_alchemy_string_fn: Callable[[Dict[str, Any]], str],
        get_sql_alchemy_connect_args_fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ):
        self.get_sql_alchemy_string_fn = get_sql_alchemy_string_fn
        self.get_sql_alchemy_connect_args_fn = get_sql_alchemy_connect_args_fn

    @abstractmethod
    def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class WorkflowWorkerInterface(ABC):
    TEMPORAL_HOST = os.getenv("TEMPORAL_HOST", "localhost")
    TEMPORAL_PORT = os.getenv("TEMPORAL_PORT", "7233")

    TEMPORAL_WORKFLOW_NAME = ClassType
    TEMPORAL_ACTIVITIES: Sequence[CallableType] = []
    PASSTHROUGH_MODULES: Sequence[str] = []

    def __init__(self, application_name: str):
        self.temporal_client = None
        self.temporal_worker = None
        self.application_name = application_name
        self.TEMPORAL_WORKER_TASK_QUEUE = f"{self.application_name}"

        self.start_worker()

    @abstractmethod
    async def run(self, *args, **kwargs) -> Dict[str, Any]:
        raise NotImplementedError

    async def start_worker(self):
        self.temporal_client = await Client.connect(
            f"{self.TEMPORAL_HOST}:{self.TEMPORAL_PORT}",
            namespace=self.application_name
        )

        self.temporal_worker = Worker(
            self.temporal_client,
            task_queue=self.TEMPORAL_WORKER_TASK_QUEUE,
            workflows=[self.TEMPORAL_WORKFLOW_NAME],
            activities=self.TEMPORAL_ACTIVITIES,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    *self.PASSTHROUGH_MODULES
                )
            )
        )

        logger.info(f"Starting worker for queue: {self.TEMPORAL_WORKER_TASK_QUEUE}")
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
