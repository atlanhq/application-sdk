import logging
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

from phoenix_sdk.dto.workflow import WorkflowConfig, WorkflowRequestPayload
from phoenix_sdk.interfaces.platform import Platform
from phoenix_sdk.logging import get_logger

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
    METADATA_EXTRACTION_TASK_QUEUE: str = ""
    WORKFLOW: ClassType
    ACTIVITIES: Sequence[CallableType] = []
    PASSTHROUGH_MODULES: Sequence[str] = []
    HOST: str = "localhost"
    PORT: str = "7233"
    NAMESPACE: str = "default"

    def __init__(
        self,
        get_sql_alchemy_string_fn: Callable,
        get_sql_alchemy_connect_args_fn: Callable,
    ):
        self.get_sql_alchemy_string_fn = get_sql_alchemy_string_fn
        self.get_sql_alchemy_connect_args_fn = get_sql_alchemy_connect_args_fn
        self.worker: Optional[Worker] = None

    async def run_workflow(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        workflow_payload = WorkflowRequestPayload(**workflow_args)

        client: Client = await Client.connect(
            f"{self.HOST}:{self.PORT}", namespace=self.NAMESPACE
        )
        workflow_id = str(uuid.uuid4())
        credential_config = workflow_payload.credentials.get_credential_config()
        credential_guid = Platform.store_credentials(credential_config)

        workflow.logger.setLevel(logging.DEBUG)
        activity.logger.setLevel(logging.DEBUG)

        config = WorkflowConfig(
            workflowId=workflow_id,
            credentialsGUID=credential_guid,
            includeFilterStr=workflow_payload.metadata.include_filter,
            excludeFilterStr=workflow_payload.metadata.exclude_filter,
            tempTableRegexStr=workflow_payload.metadata.temp_table_regex,
            outputType="JSON",
            outputPrefix="/tmp/output",
            verbose=True,
        )

        try:
            handle: WorkflowHandle[Any, Any] = await client.start_workflow(  # pyright: ignore[reportUnknownMemberType]
                self.WORKFLOW,
                config,
                id=workflow_id,
                task_queue=self.METADATA_EXTRACTION_TASK_QUEUE,
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

    async def schedule_workflow(self, workflow_args: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def run_worker(
        self,
    ) -> None:
        client: Client = await Client.connect(
            f"{self.HOST}:{self.PORT}", namespace=self.NAMESPACE
        )

        self.worker = Worker(
            client,
            task_queue=self.METADATA_EXTRACTION_TASK_QUEUE,
            workflows=[self.WORKFLOW],
            activities=self.ACTIVITIES,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    *self.PASSTHROUGH_MODULES
                )
            ),
        )

        logger.info(f"Starting worker for queue: {self.METADATA_EXTRACTION_TASK_QUEUE}")
        await self.worker.run()


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
