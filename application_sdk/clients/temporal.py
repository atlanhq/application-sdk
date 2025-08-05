import asyncio
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

from application_sdk.clients.atlan_auth import AtlanAuthClient
from application_sdk.clients.workflow import WorkflowClient
from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    DEPLOYMENT_NAME_KEY,
    MAX_CONCURRENT_ACTIVITIES,
    WORKFLOW_HOST,
    WORKFLOW_MAX_TIMEOUT_HOURS,
    WORKFLOW_NAMESPACE,
    WORKFLOW_PORT,
    WORKFLOW_TLS_ENABLED_KEY,
)
from application_sdk.inputs.secretstore import SecretStoreInput
from application_sdk.inputs.statestore import StateType
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
    """Temporal-specific implementation of WorkflowClient with simple token refresh.

    This class provides an implementation of the WorkflowClient interface for
    the Temporal workflow engine. It handles connection management, workflow
    execution, and worker creation specific to Temporal. The client uses a
    simple token refresh mechanism that updates client.rpc_metadata periodically.

    Attributes:
        client: Temporal client instance.
        worker: Temporal worker instance.
        application_name (str): Name of the application.
        worker_task_queue (str): Name of the worker task queue.
        host (str): Temporal server host.
        port (str): Temporal server port.
        namespace (str): Temporal namespace.
        _token_refresh_task: Background task for token refresh.
        _token_refresh_interval: Interval in seconds for token refresh.
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
        self.host = host if host else WORKFLOW_HOST
        self.port = port if port else WORKFLOW_PORT
        self.namespace = namespace if namespace else WORKFLOW_NAMESPACE

        self.deployment_config: Dict[str, Any] = (
            SecretStoreInput.get_deployment_secret()
        )
        self.worker_task_queue = self.get_worker_task_queue()
        self.auth_manager = AtlanAuthClient()

        # Token refresh configuration - will be determined dynamically
        self._token_refresh_interval: Optional[int] = None
        self._token_refresh_task: Optional[asyncio.Task] = None

        logger = get_logger(__name__)
        workflow.logger = logger
        activity.logger = logger

    def get_worker_task_queue(self) -> str:
        """Get the worker task queue name.

        The task queue name is derived from the application name and deployment name
        and is used to route workflow tasks to appropriate workers.

        Returns:
            str: The task queue name in format "app_name-deployment_name".
        """
        deployment_name = self.deployment_config.get(
            DEPLOYMENT_NAME_KEY, DEPLOYMENT_NAME
        )

        if deployment_name:
            return f"atlan-{self.application_name}-{deployment_name}"
        else:
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

    async def _token_refresh_loop(self) -> None:
        """Background loop that refreshes the authentication token dynamically."""
        while True:
            try:
                # Recalculate refresh interval each time in case token expiry changes
                refresh_interval = self.auth_manager.calculate_refresh_interval()

                await asyncio.sleep(refresh_interval)

                # Get fresh token
                token = await self.auth_manager.get_access_token()
                if self.client:
                    self.client.api_key = token
                logger.info("Updated client RPC metadata with fresh token")

                # Update our stored refresh interval for next iteration
                self._token_refresh_interval = (
                    self.auth_manager.calculate_refresh_interval()
                )
            except asyncio.CancelledError:
                logger.info("Token refresh loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in token refresh loop: {e}")
                # Continue the loop even if there's an error, but wait a bit
                await asyncio.sleep(60)  # Wait 1 minute before retrying

    async def load(self) -> None:
        """Connect to the Temporal server and start token refresh if needed.

        Establishes a connection to the Temporal server using the configured
        connection string and namespace. If authentication is enabled, sets up
        automatic token refresh using rpc_metadata updates.

        Raises:
            ConnectionError: If connection to the Temporal server fails.
            ValueError: If authentication is enabled but credentials are missing.
        """

        connection_options: Dict[str, Any] = {
            "target_host": self.get_connection_string(),
            "namespace": self.namespace,
            "tls": False,
        }

        connection_options["tls"] = self.deployment_config.get(
            WORKFLOW_TLS_ENABLED_KEY, False
        )
        self.worker_task_queue = self.get_worker_task_queue()

        if self.auth_manager.auth_enabled:
            # Get initial token
            token = await self.auth_manager.get_access_token()
            connection_options["api_key"] = token
            logger.info("Added initial auth token to client connection")

        # Create the client
        self.client = await Client.connect(**connection_options)

        # Start token refresh loop if auth is enabled
        if self.auth_manager.auth_enabled:
            # Calculate initial refresh interval based on token expiry
            self._token_refresh_interval = (
                self.auth_manager.calculate_refresh_interval()
            )
            self._token_refresh_task = asyncio.create_task(self._token_refresh_loop())
            logger.info(
                f"Started token refresh loop with dynamic interval (initial: {self._token_refresh_interval}s)"
            )

    async def close(self) -> None:
        """Close the Temporal client connection and stop token refresh.

        Gracefully closes the connection to the Temporal server, stops the
        token refresh loop, and clears any authentication tokens.
        """
        # Cancel token refresh task
        if self._token_refresh_task:
            self._token_refresh_task.cancel()  # cancel() is synchronous, don't await
            self._token_refresh_task = None  # Enable garbage collection
            logger.info("Stopped token refresh loop")

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
            workflow_args["credential_guid"] = await SecretStoreOutput.save_secret(
                workflow_args["credentials"]
            )
            del workflow_args["credentials"]

        workflow_id = workflow_args.get("workflow_id")
        if not workflow_id:
            # if workflow_id is not provided, create a new one
            workflow_id = workflow_args.get("argo_workflow_name", str(uuid.uuid4()))
            workflow_args.update(
                {
                    "application_name": self.application_name,
                    "workflow_id": workflow_id,
                }
            )

            await StateStoreOutput.save_state_object(
                id=workflow_id, value=workflow_args, type=StateType.WORKFLOWS
            )
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
        auto_start_token_refresh: bool = True,
    ) -> Worker:
        """Create a Temporal worker with automatic token refresh.

        Args:
            activities (Sequence[CallableType]): Activity functions to register.
            workflow_classes (Sequence[ClassType]): Workflow classes to register.
            passthrough_modules (Sequence[str]): Modules to pass through to the sandbox.
            max_concurrent_activities (int | None): Maximum number of concurrent activities.
            activity_executor (ThreadPoolExecutor | None): Executor for running activities.
            auto_start_token_refresh (bool): Whether to automatically start token refresh.
                Set to False if you've already started it via load().
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

        # Start token refresh if not already started and auth is enabled
        if (
            auto_start_token_refresh
            and self.auth_manager.auth_enabled
            and not self._token_refresh_task
        ):
            self._token_refresh_interval = (
                self.auth_manager.calculate_refresh_interval()
            )
            self._token_refresh_task = asyncio.create_task(self._token_refresh_loop())
            logger.info(
                f"Started token refresh loop with dynamic interval (initial: {self._token_refresh_interval}s)"
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
