import logging
import uuid
from abc import ABC
from typing import Any, Dict, Optional, Sequence, Type

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.types import CallableType, ClassType
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.statestore import StateStore
from application_sdk.logging import get_logger
from application_sdk.paas.eventstore import EventStore
from application_sdk.paas.eventstore.models import (
    ActivityEndEvent,
    ActivityStartEvent,
    WorkflowEndEvent,
    WorkflowStartEvent,
)
from application_sdk.workflows.resources.constants import TemporalConstants

logger = get_logger(__name__)


class EventActivityInboundInterceptor(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        EventStore.create_event(
            ActivityStartEvent(
                activity_id=activity.info().activity_id,
                activity_type=activity.info().activity_type,
            ),
            topic_name=EventStore.TOPIC_NAME,
        )
        output = await super().execute_activity(input)
        EventStore.create_event(
            ActivityEndEvent(
                activity_id=activity.info().activity_id,
                activity_type=activity.info().activity_type,
            ),
            topic_name=EventStore.TOPIC_NAME,
        )
        return output


class EventWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        with workflow.unsafe.sandbox_unrestricted():
            EventStore.create_event(
                WorkflowStartEvent(
                    workflow_name=workflow.info().workflow_type,
                    workflow_id=workflow.info().workflow_id,
                    workflow_run_id=workflow.info().run_id,
                ),
                topic_name=EventStore.TOPIC_NAME,
            )
        output = await super().execute_workflow(input)
        with workflow.unsafe.sandbox_unrestricted():
            EventStore.create_event(
                WorkflowEndEvent(
                    workflow_name=workflow.info().workflow_type,
                    workflow_id=workflow.info().workflow_id,
                    workflow_run_id=workflow.info().run_id,
                    workflow_output=output or {},
                ),
                topic_name=EventStore.TOPIC_NAME,
            )
        return output


class EventInterceptor(Interceptor):
    """Temporal Interceptor class which will report workflow & activity exceptions to Sentry"""

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Implementation of
        :py:meth:`temporalio.worker.Interceptor.intercept_activity`.
        """
        return EventActivityInboundInterceptor(super().intercept_activity(next))

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        return EventWorkflowInboundInterceptor


class ResourceInterface(ABC):
    def __init__(self):
        pass

    async def load(self):
        pass

    def set_credentials(self, credentials: Dict[str, Any]):
        pass


class TemporalConfig:
    host = TemporalConstants.HOST.value
    port = TemporalConstants.PORT.value
    application_name = TemporalConstants.APPLICATION_NAME.value
    # FIXME: causes issue with different namespace, TBR.
    namespace: str = TemporalConstants.NAMESPACE.value

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

    async def start_workflow(self, workflow_args: Any, workflow_class: Any):
        if "credentials" in workflow_args:
            # remove credentials from workflow_args and add reference to credentials
            workflow_args["credential_guid"] = StateStore.store_credentials(
                workflow_args["credentials"]
            )
            del workflow_args["credentials"]

        workflow_id = workflow_args.get("workflow_id")
        if not workflow_id:
            # if workflow_id is not provided, create a new one
            workflow_id = str(uuid.uuid4())

            workflow_args.update(
                {
                    "application_name": self.config.application_name,
                    "workflow_id": workflow_id,
                    "output_prefix": "/tmp/output",
                }
            )

            StateStore.store_configuration(workflow_id, workflow_args)

            logger.info(f"Created workflow config with ID: {workflow_id}")

        workflow.logger.setLevel(logging.DEBUG)
        activity.logger.setLevel(logging.DEBUG)

        try:
            handle = await self.client.start_workflow(
                workflow_class,
                workflow_args,
                id=workflow_id,
                task_queue=self.worker_task_queue,
                cron_schedule=workflow_args.get("cron_schedule", ""),
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
        self,
        activities: Sequence[CallableType],
        workflow_classes: Sequence[ClassType],
        passthrough_modules: Sequence[str],
    ) -> Worker:
        if not self.client:
            raise ValueError("Client is not loaded")

        return Worker(
            self.client,
            task_queue=self.worker_task_queue,
            workflows=workflow_classes,
            activities=activities,
            workflow_runner=SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    *passthrough_modules
                )
            ),
            interceptors=[EventInterceptor()],
        )
