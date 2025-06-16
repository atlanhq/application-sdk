import uuid
from concurrent.futures import ThreadPoolExecutor
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

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.constants import (
    APPLICATION_NAME,
    MAX_CONCURRENT_ACTIVITIES,
    WORKFLOW_HOST,
    WORKFLOW_MAX_TIMEOUT_HOURS,
    WORKFLOW_NAMESPACE,
    WORKFLOW_PORT,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.eventstore import (
    ApplicationEventNames,
    Event,
    EventMetadata,
    EventStore,
    EventTypes,
    WorkflowStates,
)
from application_sdk.outputs.secretstore import SecretStoreOutput
from application_sdk.outputs.statestore import StateStoreOutput
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)

TEMPORAL_NOT_FOUND_FAILURE = (
    "type.googleapis.com/temporal.api.errordetails.v1.NotFoundFailure"
)


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
        event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.ACTIVITY_START.value,
            data={},
        )

        EventStore.publish_event(event)

        output = None
        try:
            output = await super().execute_activity(input)
        except Exception as e:
            end_event = Event(
                event_type=EventTypes.APPLICATION_EVENT.value,
                event_name=ApplicationEventNames.ACTIVITY_END.value,
                data={},
            )
            EventStore.publish_event(end_event)
            raise e

        end_event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.ACTIVITY_END.value,
            data={},
        )
        EventStore.publish_event(end_event)
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
            EventStore.publish_event(
                Event(
                    metadata=EventMetadata(workflow_state=WorkflowStates.RUNNING.value),
                    event_type=EventTypes.APPLICATION_EVENT.value,
                    event_name=ApplicationEventNames.WORKFLOW_START.value,
                    data={},
                )
            )
        output = None
        try:
            output = await super().execute_workflow(input)
        except Exception as e:
            with workflow.unsafe.sandbox_unrestricted():
                EventStore.publish_event(
                    Event(
                        metadata=EventMetadata(
                            workflow_state=WorkflowStates.FAILED.value
                        ),
                        event_type=EventTypes.APPLICATION_EVENT.value,
                        event_name=ApplicationEventNames.WORKFLOW_END.value,
                        data={},
                    ),
                )
            raise e

        with workflow.unsafe.sandbox_unrestricted():
            EventStore.publish_event(
                Event(
                    metadata=EventMetadata(
                        workflow_state=WorkflowStates.COMPLETED.value
                    ),
                    event_type=EventTypes.APPLICATION_EVENT.value,
                    event_name=ApplicationEventNames.WORKFLOW_END.value,
                    data={
                        "workflow_id": workflow.info().workflow_id,
                        "workflow_run_id": workflow.info().run_id,
                    },
                ),
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


class TemporalWorkflowClient(WorkflowClient):
    """Temporal-specific implementation of WorkflowClient.

    This class provides an implementation of the WorkflowClient interface for
    the Temporal workflow engine. It handles connection management, workflow
    execution, and worker creation specific to Temporal.

    Attributes:
        client: Temporal client instance.
        worker: Temporal worker instance.
        application_name (str): Name of the application.
        worker_task_queue (str): Name of the worker task queue.
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
        """Initialize the Temporal workflow client.

        Args:
            host (str | None, optional): Temporal server host. Defaults to
                environment variable WORKFLOW_HOST.
            port (str | None, optional): Temporal server port. Defaults to
                environment variable WORKFLOW_PORT.
            application_name (str | None, optional): Name of the application.
                Defaults to environment variable APPLICATION_NAME.
            namespace (str | None, optional): Temporal namespace. Defaults to
                "default" or environment variable WORKFLOW_NAMESPACE.
        """
        self.client = None
        self.worker = None
        self.application_name = (
            application_name if application_name else APPLICATION_NAME
        )
        self.worker_task_queue = self.get_worker_task_queue()
        self.host = host if host else WORKFLOW_HOST
        self.port = port if port else WORKFLOW_PORT
        self.namespace = namespace if namespace else WORKFLOW_NAMESPACE

        logger = get_logger(__name__)
        workflow.logger = logger
        activity.logger = logger

    def get_worker_task_queue(self) -> str:
        """Get the worker task queue name.

        The task queue name is derived from the application name and is used
        to route workflow tasks to appropriate workers.

        Returns:
            str: The task queue name, which is the same as the application name.
        """
        return self.application_name

    def get_connection_string(self) -> str:
        """Get the Temporal server connection string.

        Constructs a connection string from the configured host and port in
        the format "host:port".

        Returns:
            str: The connection string for the Temporal server.
        """
        return f"{self.host}:{self.port}"

    def get_namespace(self) -> str:
        """Get the Temporal namespace.

        Returns the configured namespace where workflows will be executed.
        The namespace provides isolation between different environments or
        applications.

        Returns:
            str: The Temporal namespace.
        """
        return self.namespace

    async def load(self) -> None:
        """Connect to the Temporal server.

        Establishes a connection to the Temporal server using the configured
        connection string and namespace.

        Raises:
            ConnectionError: If connection to the Temporal server fails.
        """
        self.client = await Client.connect(
            self.get_connection_string(),
            namespace=self.namespace,
        )

    async def close(self) -> None:
        """Close the Temporal client connection.

        Gracefully closes the connection to the Temporal server. This is a
        no-op if the connection is already closed.
        """
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
            ValueError: If the client is not loaded.
        """
        if "credentials" in workflow_args:
            # remove credentials from workflow_args and add reference to credentials
            workflow_args["credential_guid"] = SecretStoreOutput.store_credentials(
                workflow_args["credentials"]
            )
            del workflow_args["credentials"]

        workflow_id = workflow_args.get("workflow_id")
        output_prefix = workflow_args.get("output_prefix", "/tmp/output")
        if not workflow_id:
            # if workflow_id is not provided, create a new one
            workflow_id = workflow_args.get("argo_workflow_name", str(uuid.uuid4()))

            workflow_args.update(
                {
                    "application_name": self.application_name,
                    "workflow_id": workflow_id,
                    "output_prefix": output_prefix,
                }
            )

            StateStoreOutput.store_configuration(workflow_id, workflow_args)

            logger.info(f"Created workflow config with ID: {workflow_id}")

        try:
            # Pass the full workflow_args to the workflow
            if not self.client:
                raise ValueError("Client is not loaded")
            handle = await self.client.start_workflow(
                workflow_class,  # type: ignore
                args=[{"workflow_id": workflow_id}],
                id=workflow_id,
                task_queue=self.worker_task_queue,
                cron_schedule=workflow_args.get("cron_schedule", ""),
                execution_timeout=WORKFLOW_MAX_TIMEOUT_HOURS,
            )
            logger.info(f"Workflow started: {handle.id} {handle.result_run_id}")

            return {
                "workflow_id": handle.id,
                "run_id": handle.result_run_id,
                "handle": handle,  # Return the handle so it can be used to get the result
            }
        except WorkflowFailureError as e:
            logger.error(f"Workflow failure: {e}")
            raise e

    async def stop_workflow(self, workflow_id: str, run_id: str) -> None:
        """Stop a workflow execution.

        Args:
            workflow_id (str): The ID of the workflow.
            run_id (str): The run ID of the workflow.

        Raises:
            ValueError: If the client is not loaded.
        """
        if not self.client:
            raise ValueError("Client is not loaded")
        try:
            workflow_handle = self.client.get_workflow_handle(
                workflow_id, run_id=run_id
            )
            await workflow_handle.terminate()
        except Exception as e:
            logger.error(f"Error terminating workflow {workflow_id} {run_id}: {e}")
            raise Exception(f"Error terminating workflow {workflow_id} {run_id}: {e}")

    def create_worker(
        self,
        activities: Sequence[CallableType],
        workflow_classes: Sequence[ClassType],
        passthrough_modules: Sequence[str],
        max_concurrent_activities: Optional[int] = MAX_CONCURRENT_ACTIVITIES,
        activity_executor: Optional[ThreadPoolExecutor] = None,
    ) -> Worker:
        """Create a Temporal worker.

        Args:
            activities (Sequence[CallableType]): Activity functions to register.
            workflow_classes (Sequence[ClassType]): Workflow classes to register.
            passthrough_modules (Sequence[str]): Modules to pass through to the sandbox.
            max_concurrent_activities (int | None): Maximum number of concurrent activities.
            activity_executor (ThreadPoolExecutor | None): Executor for running activities.
        Returns:
            Worker: The created worker instance.

        Raises:
            ValueError: If the client is not loaded.
        """
        if not self.client:
            raise ValueError("Client is not loaded")

        # Always provide an executor if none given
        if activity_executor is None:
            activity_executor = ThreadPoolExecutor(
                max_workers=max_concurrent_activities or 5,
                thread_name_prefix="activity-pool-",
            )

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
            max_concurrent_activities=max_concurrent_activities,
            activity_executor=activity_executor,
            interceptors=[EventInterceptor()],
        )

    async def get_workflow_run_status(
        self,
        workflow_id: str,
        run_id: Optional[str] = None,
        include_last_executed_run_id: bool = False,
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

        try:
            workflow_handle = self.client.get_workflow_handle(
                workflow_id, run_id=run_id
            )
            workflow_execution = await workflow_handle.describe()
            execution_info = workflow_execution.raw_description.workflow_execution_info

            workflow_info = {
                "workflow_id": workflow_id,
                "run_id": run_id,
                "status": WorkflowExecutionStatus(execution_info.status).name,
                "execution_duration_seconds": execution_info.execution_duration.ToSeconds(),
            }
            if include_last_executed_run_id:
                workflow_info["last_executed_run_id"] = (
                    execution_info.root_execution.run_id
                )
            return workflow_info
        except Exception as e:
            if (
                hasattr(e, "grpc_status")
                and hasattr(e.grpc_status, "details")
                and e.grpc_status.details[0].type_url == TEMPORAL_NOT_FOUND_FAILURE
            ):
                return {
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "status": "NOT_FOUND",
                    "execution_duration_seconds": 0,
                }
            logger.error(f"Error getting workflow status: {e}")
            raise Exception(
                f"Error getting workflow status for {workflow_id} {run_id}: {e}"
            )
