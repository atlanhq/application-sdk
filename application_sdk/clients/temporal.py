import logging
import uuid
from typing import Any, Dict, Optional, Sequence, Type

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
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

from application_sdk.clients import ClientInterface
from application_sdk.clients.constants import TemporalConstants
from application_sdk.common.constants import ApplicationConstants
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.statestore import StateStore
from application_sdk.outputs.eventstore import (
    ActivityEndEvent,
    ActivityStartEvent,
    EventStore,
    WorkflowEndEvent,
    WorkflowStartEvent,
)
from application_sdk.workflows import WorkflowInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class EventActivityInboundInterceptor(ActivityInboundInterceptor):
    """Interceptor for tracking activity execution events.

    This interceptor captures the start and end of activity executions,
    creating events that can be used for monitoring and tracking.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute an activity with event tracking.

        Args:
            input (ExecuteActivityInput): The activity execution input.

        Returns:
            Any: The result of the activity execution.
        """
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
    """Interceptor for tracking workflow execution events.

    This interceptor captures the start and end of workflow executions,
    creating events that can be used for monitoring and tracking.
    """

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute a workflow with event tracking.

        Args:
            input (ExecuteWorkflowInput): The workflow execution input.

        Returns:
            Any: The result of the workflow execution.
        """
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
    """Temporal interceptor for event tracking.

    This interceptor provides event tracking capabilities for both
    workflow and activity executions.
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Intercept activity executions.

        Args:
            next (ActivityInboundInterceptor): The next interceptor in the chain.

        Returns:
            ActivityInboundInterceptor: The activity interceptor.
        """
        return EventActivityInboundInterceptor(super().intercept_activity(next))

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class.

        Args:
            input (WorkflowInterceptorClassInput): The interceptor input.

        Returns:
            Optional[Type[WorkflowInboundInterceptor]]: The workflow interceptor class.
        """
        return EventWorkflowInboundInterceptor


class TemporalClient(ClientInterface):
    """Client for interacting with Temporal workflow service.

    This class provides functionality for managing workflow executions,
    including starting workflows, creating workers, and checking workflow status.

    Attributes:
        client: Temporal client instance.
        worker: Temporal worker instance.
        application_name (str): Name of the application.
        worker_task_queue (str): Task queue for the worker.
        host (str): Temporal server host.
        port (str): Temporal server port.
        namespace (str): Temporal namespace.
    """

    def __init__(
        self,
        host: str | None = None,
        port: str | None = None,
        application_name: str | None = None,
        namespace: str | None = "default",
    ):
        """Initialize the Temporal client.

        Args:
            host (str | None, optional): Temporal server host. Defaults to None.
            port (str | None, optional): Temporal server port. Defaults to None.
            application_name (str | None, optional): Application name. Defaults to None.
            namespace (str | None, optional): Temporal namespace. Defaults to "default".
        """
        self.client = None
        self.worker = None
        self.application_name = (
            application_name
            if application_name
            else ApplicationConstants.APPLICATION_NAME.value
        )
        self.worker_task_queue = self.get_worker_task_queue()
        self.host = host if host else TemporalConstants.HOST.value
        self.port = port if port else TemporalConstants.PORT.value
        self.namespace = namespace if namespace else TemporalConstants.NAMESPACE.value

        workflow.logger = AtlanLoggerAdapter(logging.getLogger(__name__))
        activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))

    def get_worker_task_queue(self) -> str:
        """Get the worker task queue name.

        Returns:
            str: The task queue name.
        """
        return self.application_name

    def get_connection_string(self) -> str:
        """Get the Temporal server connection string.

        Returns:
            str: The connection string.
        """
        return f"{self.host}:{self.port}"

    def get_namespace(self) -> str:
        """Get the Temporal namespace.

        Returns:
            str: The namespace.
        """
        return self.namespace

    async def load(self) -> None:
        """Connect to the Temporal server."""
        self.client = await Client.connect(
            self.get_connection_string(),
            namespace=self.namespace,
        )

    async def close(self) -> None:
        """Close the Temporal client connection."""
        return

    async def start_workflow(
        self, workflow_args: Dict[str, Any], workflow_class: Type[WorkflowInterface]
    ) -> Dict[str, Any]:
        """Start a workflow execution.

        Args:
            workflow_args (Dict[str, Any]): Arguments for the workflow.
            workflow_class (Type[WorkflowInterface]): The workflow class to execute.

        Returns:
            Dict[str, Any]: A dictionary containing:
                - workflow_id (str): The ID of the started workflow
                - run_id (str): The run ID of the workflow execution

        Raises:
            WorkflowFailureError: If the workflow fails to start.
        """
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
                    "application_name": self.application_name,
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
                {
                    "workflow_id": workflow_id,
                },
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
        """Create a Temporal worker.

        Args:
            activities (Sequence[CallableType]): Activity functions to register.
            workflow_classes (Sequence[ClassType]): Workflow classes to register.
            passthrough_modules (Sequence[str]): Modules to pass through to the sandbox.

        Returns:
            Worker: The created worker instance.

        Raises:
            ValueError: If the client is not loaded.
        """
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

    async def get_workflow_run_status(
        self, workflow_id: str, run_id: str
    ) -> Dict[str, Any]:
        """Get the status of a workflow run.

        Args:
            workflow_id (str): The workflow ID.
            run_id (str): The run ID.

        Returns:
            Dict[str, Any]: A dictionary containing:
                - workflow_id (str): The workflow ID
                - run_id (str): The run ID
                - status (str): The workflow execution status
                - execution_duration_seconds (int): Duration in seconds

        Raises:
            ValueError: If the client is not loaded.
            Exception: If there's an error getting the workflow status.
        """
        if not self.client:
            raise ValueError("Client is not loaded")

        workflow_handle = self.client.get_workflow_handle(workflow_id, run_id=run_id)
        try:
            workflow_execution = await workflow_handle.describe()
            execution_info = workflow_execution.raw_description.workflow_execution_info
        except Exception as e:
            logger.error(f"Error getting workflow status: {e}")
            raise Exception(
                f"Error getting workflow status for {workflow_id} {run_id}: {e}"
            )

        workflow_info = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "status": WorkflowExecutionStatus(execution_info.status).name,
            "execution_duration_seconds": execution_info.execution_duration.ToSeconds(),
        }
        return workflow_info
