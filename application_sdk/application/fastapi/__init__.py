from typing import Any, Callable, Dict, List, Optional, Type, cast

from application_sdk.common.constants import ApplicationConstants
from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from uvicorn import Config, Server

from application_sdk.application import AtlanApplicationInterface
from application_sdk.application.fastapi.middleware.logmiddleware import LogMiddleware
from application_sdk.application.fastapi.models import (
    FetchMetadataRequest,
    FetchMetadataResponse,
    PreflightCheckRequest,
    PreflightCheckResponse,
    TestAuthRequest,
    TestAuthResponse,
    WorkflowConfigRequest,
    WorkflowConfigResponse,
    WorkflowData,
    WorkflowRequest,
    WorkflowResponse,
)
from application_sdk.application.fastapi.routers.server import get_server_router
from application_sdk.application.fastapi.utils import internal_server_error_handler
from application_sdk.clients.workflow import WorkflowClient, WorkflowConstants
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.utils import get_workflow_config, update_workflow_config
from application_sdk.docgen import AtlanDocsGenerator
from application_sdk.handlers import HandlerInterface
from application_sdk.outputs.eventstore import AtlanEvent, EventStore
from application_sdk.worker import Worker
from application_sdk.workflows import WorkflowInterface

try:
    from langgraph.graph import StateGraph

    from application_sdk.activities.agents.langgraph import register_graph_builder
    from application_sdk.agents import AgentState, LangGraphWorkflow

    langgraph_available = True
except ImportError:
    # Create placeholders for type checking when langgraph is not available
    StateGraph = Any  # type: ignore
    AgentState = Any  # type: ignore
    LangGraphWorkflow = Any  # type: ignore

    def register_graph_builder(name: str, builder_func: Any) -> None:
        """Placeholder for register_graph_builder when langgraph is not available."""
        pass

    langgraph_available = False

logger = get_logger(__name__)


class WorkflowTrigger(BaseModel):
    workflow_class: Optional[Type[WorkflowInterface]] = None
    model_config = {"arbitrary_types_allowed": True}


class HttpWorkflowTrigger(WorkflowTrigger):
    endpoint: str = "/start"
    methods: List[str] = ["POST"]


class EventWorkflowTrigger(WorkflowTrigger):
    should_trigger_workflow: Callable[[Any], bool]


class AgentRequest(BaseModel):
    """Request model for agent endpoints."""

    user_query: str
    workflow_state: Optional[Dict[str, Any]] = None
    schedule_to_close_timeout: Optional[int] = None  # timeout in seconds
    heartbeat_timeout: Optional[int] = None  # timeout in seconds


class Application(AtlanApplicationInterface):
    """A FastAPI Application implementation of the Atlan Application Interface.

    This class provides a FastAPI-based web application that handles workflow management,
    authentication, metadata operations, and event processing. It supports both HTTP and
    event-based workflow triggers.

    Attributes:
        app (FastAPI): The main FastAPI application instance.
        workflow_client (Optional[WorkflowClient]): Client for interacting with Temporal workflows.
        workflow_router (APIRouter): Router for workflow-related endpoints.
        pubsub_router (APIRouter): Router for pub/sub operations.
        events_router (APIRouter): Router for event handling.
        docs_directory_path (str): Path to documentation source directory.
        docs_export_path (str): Path where documentation will be exported.
        workflows (List[WorkflowInterface]): List of registered workflows.
        event_triggers (List[EventWorkflowTrigger]): List of event-based workflow triggers.

    Args:
        lifespan: Optional lifespan manager for the FastAPI application.
        handler (Optional[HandlerInterface]): Handler for processing application operations.
        workflow_client (Optional[WorkflowClient]): Client for Temporal workflow operations.
    """

    app: FastAPI
    workflow_client: Optional[WorkflowClient]

    workflow_router: APIRouter = APIRouter()
    pubsub_router: APIRouter = APIRouter()
    events_router: APIRouter = APIRouter()

    docs_directory_path: str = "docs"
    docs_export_path: str = "dist"

    workflows: List[WorkflowInterface] = []
    event_triggers: List[EventWorkflowTrigger] = []

    def __init__(
        self,
        lifespan=None,
        handler: Optional[HandlerInterface] = None,
        workflow_client: Optional[WorkflowClient] = None,
    ):
        """Initialize the FastAPI application.

        Args:
            lifespan: Optional lifespan manager for the FastAPI application.
            handler (Optional[HandlerInterface]): Handler for processing application operations.
            workflow_client (Optional[WorkflowClient]): Client for Temporal workflow operations.
        """
        self.app = FastAPI(lifespan=lifespan)
        self.app.add_exception_handler(
            status.HTTP_500_INTERNAL_SERVER_ERROR, internal_server_error_handler
        )
        self.templates = Jinja2Templates(directory="frontend/templates")
        self.handler = handler
        self.workflow_client = workflow_client
        self.app.add_middleware(LogMiddleware)
        self.register_routers()
        self.setup_atlan_docs()
        super().__init__(handler)

    def setup_atlan_docs(self):
        """Set up and serve Atlan documentation.

        Generates documentation using AtlanDocsGenerator and mounts it at the /atlandocs endpoint.
        Any exceptions during documentation generation are logged as warnings.
        """
        docs_generator = AtlanDocsGenerator(
            docs_directory_path=self.docs_directory_path,
            export_path=self.docs_export_path,
        )
        try:
            docs_generator.export()

            self.app.mount(
                "/atlandocs",
                StaticFiles(directory=f"{self.docs_export_path}/site", html=True),
                name="atlandocs",
            )
        except Exception as e:
            logger.warning(str(e))

    def register_routers(self):
        """Register all routers with the FastAPI application.

        Registers routes and includes all routers with their respective prefixes:
        - Server router
        - Workflow router (/workflows/v1)
        - Pubsub router (/dapr)
        - Events router (/events/v1)
        """
        # Register all routes first
        self.register_routes()

        # Then include all routers
        self.app.include_router(get_server_router())
        self.app.include_router(self.workflow_router, prefix="/workflows/v1")
        self.app.include_router(self.pubsub_router, prefix="/dapr")
        self.app.include_router(self.events_router, prefix="/events/v1")


    async def home(self, request: Request) -> HTMLResponse:
        return self.templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "app_dashboard_http_port": ApplicationConstants.APP_DASHBOARD_PORT.value,
                "app_dashboard_http_host": ApplicationConstants.APP_DASHBOARD_HOST.value,
                "app_http_port": ApplicationConstants.APP_PORT.value,
                "app_http_host": ApplicationConstants.APP_HOST.value,
                "tenant_id": ApplicationConstants.TENANT_ID.value,
                "app_name": ApplicationConstants.APPLICATION_NAME.value,
                "workflow_ui_host": WorkflowConstants.UI_HOST.value,
                "workflow_ui_port": WorkflowConstants.UI_PORT.value,
            },
        )

    def register_workflow(
        self, workflow_class: Type[WorkflowInterface], triggers: List[WorkflowTrigger]
    ):
        """Register a workflow with its associated triggers.

        Args:
            workflow_class (Type[WorkflowInterface]): The workflow class to register.
            triggers (List[WorkflowTrigger]): List of triggers (HTTP or Event) that can start the workflow.

        Raises:
            Exception: If temporal client is not initialized for HTTP triggers.
        """
        for trigger in triggers:
            trigger.workflow_class = workflow_class

            if isinstance(trigger, HttpWorkflowTrigger):

                async def start_workflow(body: WorkflowRequest):
                    if not self.workflow_client:
                        raise Exception("Temporal client not initialized")

                    workflow_data = await self.workflow_client.start_workflow(
                        body.model_dump(), workflow_class=workflow_class
                    )
                    return WorkflowResponse(
                        success=True,
                        message="Workflow started successfully",
                        data=WorkflowData(
                            workflow_id=workflow_data.get("workflow_id") or "",
                            run_id=workflow_data.get("run_id") or "",
                        ),
                    )

                self.workflow_router.add_api_route(
                    trigger.endpoint,
                    start_workflow,
                    methods=trigger.methods,
                    response_model=WorkflowResponse,
                )
                self.app.include_router(self.workflow_router, prefix="/workflows/v1")
            elif isinstance(trigger, EventWorkflowTrigger):
                self.event_triggers.append(trigger)

    def register_routes(self):
        """
        Method to register the routes for the FastAPI application
        """

        self.workflow_router.add_api_route(
            "/auth",
            self.test_auth,
            methods=["POST"],
            response_model=TestAuthResponse,
        )
        self.workflow_router.add_api_route(
            "/metadata",
            self.fetch_metadata,
            methods=["POST"],
            response_model=FetchMetadataResponse,
        )
        self.workflow_router.add_api_route(
            "/check",
            self.preflight_check,
            methods=["POST"],
            response_model=PreflightCheckResponse,
        )
        self.workflow_router.add_api_route(
            "/config/{config_id}",
            self.get_workflow_config,
            methods=["GET"],
            response_model=WorkflowConfigResponse,
        )

        self.workflow_router.add_api_route(
            "/config/{config_id}",
            self.update_workflow_config,
            methods=["POST"],
            response_model=WorkflowConfigResponse,
        )

        self.workflow_router.add_api_route(
            "/status/{workflow_id}/{run_id:path}",
            self.get_workflow_run_status,
            description="Get the status of the current or last workflow run",
            methods=["GET"],
        )

        self.workflow_router.add_api_route(
            "/stop/{workflow_id}/{run_id:path}",
            self.stop_workflow,
            methods=["POST"],
        )

        self.pubsub_router.add_api_route(
            "/subscribe",
            self.get_dapr_subscriptions,
            methods=["GET"],
            response_model="list",
        )

        self.events_router.add_api_route(
            "/event",
            self.on_event,
            methods=["POST"],
        )


    def register_ui_routes(self):
        """Register the UI routes for the FastAPI application."""
        self.app.get("/")(self.home)
        # Mount static files
        self.app.mount("/", StaticFiles(directory="frontend/static"), name="static")

    async def get_dapr_subscriptions(
        self,
    ) -> List[dict[str, Any]]:
        """Get Dapr pubsub subscriptions configuration.

        Returns:
            List[dict[str, Any]]: List of Dapr subscription configurations including
                pubsub name, topic, and routing rules.
        """
        return [
            {
                "pubsubname": EventStore.EVENT_STORE_NAME,
                "topic": EventStore.TOPIC_NAME,
                "routes": {"rules": [{"path": "events/v1/event"}]},
            }
        ]

    async def on_event(self, event: dict[str, Any]):
        """Handle incoming events and trigger appropriate workflows.

        Args:
            event (dict[str, Any]): The event data to process.

        Raises:
            Exception: If temporal client is not initialized.
        """
        if not self.workflow_client:
            raise Exception("Temporal client not initialized")

        logger.info("Received event {}", event)
        for trigger in self.event_triggers:
            if trigger.should_trigger_workflow(AtlanEvent(**event)):
                logger.info(
                    "Triggering workflow {} with event {}",
                    trigger.workflow_class,
                    event,
                )

                await self.workflow_client.start_workflow(
                    workflow_args=event, workflow_class=trigger.workflow_class
                )

    async def test_auth(self, body: TestAuthRequest) -> TestAuthResponse:
        """Test authentication credentials.

        Args:
            body (TestAuthRequest): Request containing authentication credentials.

        Returns:
            TestAuthResponse: Response indicating authentication success.
        """
        await self.handler.load(body.model_dump())
        await self.handler.test_auth()
        return TestAuthResponse(success=True, message="Authentication successful")

    async def fetch_metadata(self, body: FetchMetadataRequest) -> FetchMetadataResponse:
        """Fetch metadata based on request parameters.

        Args:
            body (FetchMetadataRequest): Request containing metadata fetch parameters.

        Returns:
            FetchMetadataResponse: Response containing the requested metadata.
        """
        await self.handler.load(body.model_dump())
        metadata = await self.handler.fetch_metadata(
            metadata_type=body.root["type"], database=body.root["database"]
        )
        return FetchMetadataResponse(success=True, data=metadata)

    async def preflight_check(
        self, body: PreflightCheckRequest
    ) -> PreflightCheckResponse:
        """Perform preflight checks with provided configuration.

        Args:
            body (PreflightCheckRequest): Request containing preflight check parameters.

        Returns:
            PreflightCheckResponse: Response containing preflight check results.
        """
        await self.handler.load(body.credentials)
        preflight_check = await self.handler.preflight_check(body.model_dump())
        return PreflightCheckResponse(success=True, data=preflight_check)

    def get_workflow_config(self, config_id: str) -> WorkflowConfigResponse:
        """Retrieve workflow configuration by ID.

        Args:
            config_id (str): The ID of the workflow configuration to retrieve.

        Returns:
            WorkflowConfigResponse: Response containing the workflow configuration.
        """
        config = get_workflow_config(config_id)
        return WorkflowConfigResponse(
            success=True,
            message="Workflow configuration fetched successfully",
            data=config,
        )

    async def get_workflow_run_status(
        self, workflow_id: str, run_id: str
    ) -> JSONResponse:
        """Get the status of a specific workflow run.

        Args:
            workflow_id (str): The ID of the workflow.
            run_id (str): The ID of the specific run.

        Returns:
            JSONResponse: Response containing the workflow run status.

        Raises:
            Exception: If temporal client is not initialized.
        """
        if not self.workflow_client:
            raise Exception("Temporal client not initialized")

        workflow_status = await self.workflow_client.get_workflow_run_status(
            workflow_id,
            run_id,
            include_last_executed_run_id=True,
        )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Workflow status fetched successfully",
                "data": workflow_status,
            },
        )

    def update_workflow_config(
        self, config_id: str, body: WorkflowConfigRequest
    ) -> WorkflowConfigResponse:
        """Update workflow configuration.

        Args:
            config_id (str): The ID of the workflow configuration to update.
            body (WorkflowConfigRequest): The new configuration data.

        Returns:
            WorkflowConfigResponse: Response containing the updated configuration.
        """
        # note: it's assumed that the preflight check is successful if the config is being updated
        config = update_workflow_config(config_id, body.model_dump())
        return WorkflowConfigResponse(
            success=True,
            message="Workflow configuration updated successfully",
            data=config,
        )

    async def stop_workflow(self, workflow_id: str, run_id: str) -> JSONResponse:
        """Stop a running workflow.

        Args:
            workflow_id (str): The ID of the workflow to stop.
            run_id (str): The ID of the specific run to stop.

        Returns:
            JSONResponse: Response indicating success of the stop operation.

        Raises:
            Exception: If temporal client is not initialized.
        """
        if not self.workflow_client:
            raise Exception("Temporal client not initialized")

        await self.workflow_client.stop_workflow(workflow_id, run_id)
        return JSONResponse(status_code=status.HTTP_200_OK, content={"success": True})

    async def start(
        self, host: str = ApplicationConstants.APP_HOST.value,
        port: int = ApplicationConstants.APP_PORT.value,
    ) -> None:
        """Start the FastAPI application server.

        Args:
            host (str, optional): Host address to bind to. Defaults to "0.0.0.0".
            port (int, optional): Port to listen on. Defaults to 8000.
        """
        self.register_ui_routes()

        logger.info(f"Starting application on {host}:{port}")
        server = Server(
            Config(
                app=self.app,
                host=host,
                port=port,
            )
        )
        await server.serve()


class AgentApplication(Application):
    """FastAPI Application implementation with agent capabilities."""

    agent_router: APIRouter = APIRouter()
    workflow_client: Optional[WorkflowClient] = None
    worker: Optional[Worker] = None
    workflow_handles: Dict[str, Any] = {}
    graph_builder_name: str

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if not langgraph_available:
            raise ImportError(
                "LangGraph is required for FastAPIAgentApplication. "
                "Please install it with 'pip install application-sdk[langgraph_agent]' or use the langgraph_agent extra."
            )
        super().__init__(*args, **kwargs)

    def register_routers(self) -> None:
        """Register all routers including the agent router."""
        self.register_routes()
        self.app.include_router(self.agent_router, prefix="/api/v1/agent")
        super().register_routers()

    def register_routes(self) -> None:
        """Register agent-specific routes."""
        self.agent_router.add_api_route(
            "/query",
            self.process_query,
            methods=["POST"],
        )
        self.agent_router.add_api_route(
            "/result/{workflow_id}",
            self.get_workflow_result,
            methods=["GET"],
        )
        super().register_routes()

    async def process_query(self, request: AgentRequest) -> Dict[str, Any]:
        """Process an agent query.

        Args:
            request: The agent request containing the user query and optional workflow state.
                In the request:
                - user_query: The user's query
                - workflow_state: The state of the workflow
                - schedule_to_close_timeout: Optional timeout in seconds for activity completion
                - heartbeat_timeout: Optional timeout in seconds for activity heartbeat

        Returns:
            Dict containing the workflow ID and run ID.
        """
        if not self.workflow_client:
            raise Exception("Temporal client not initialized")

        # Use provided state or create default state
        state = request.workflow_state or AgentState(messages=[])

        workflow_input = {
            "user_query": request.user_query,
            "state": state,
            "graph_builder_name": self.graph_builder_name,
            "schedule_to_close_timeout": request.schedule_to_close_timeout,
            "heartbeat_timeout": request.heartbeat_timeout,
        }

        # Use cast to assure the type checker that LangGraphWorkflow is a valid workflow class
        response = await self.workflow_client.start_workflow(
            workflow_class=cast(Type[WorkflowInterface], LangGraphWorkflow),
            workflow_args=workflow_input,
        )

        if "handle" in response:
            self.workflow_handles[response["workflow_id"]] = response["handle"]

        return {
            "success": True,
            "message": "Workflow started successfully",
            "data": {
                "workflow_id": response.get("workflow_id", ""),
                "run_id": response.get("run_id", ""),
            },
        }

    async def get_workflow_result(self, workflow_id: str) -> Dict[str, Any]:
        """Get the result of a completed workflow.

        Args:
            workflow_id: The ID of the workflow to get results for.

        Returns:
            Dict containing the workflow result.
        """
        if workflow_id not in self.workflow_handles:
            return {
                "success": False,
                "message": "Workflow not found or already completed",
                "data": None,
            }

        try:
            handle = self.workflow_handles[workflow_id]
            result = await handle.result()
            del self.workflow_handles[workflow_id]

            return {
                "success": True,
                "message": "Workflow completed successfully",
                "data": result,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Error getting workflow result: {str(e)}",
                "data": None,
            }

    async def setup_worker(self, workflow_client: WorkflowClient) -> Worker:
        """Set up and start the Temporal worker.

        Args:
            workflow_client: The Temporal client instance.

        Returns:
            The initialized Worker instance.
        """
        self.worker = Worker(
            workflow_client=workflow_client,
            workflow_classes=[cast(Type[WorkflowInterface], LangGraphWorkflow)],
            temporal_activities=LangGraphWorkflow.get_activities(
                LangGraphWorkflow.activities_cls()
            ),
            max_concurrent_activities=5,
        )
        return self.worker

    def register_graph(
        self,
        state_graph_builder: Callable[..., StateGraph],
        graph_builder_name: str = "workflow",
    ) -> None:
        """Register a graph with a given name.

        Args:
            state_graph_builder: Function that returns a LangGraph StateGraph
            graph_builder_name: Name to register the graph builder under
        """
        register_graph_builder(graph_builder_name, state_graph_builder)
        self.graph_builder_name = graph_builder_name
