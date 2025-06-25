import time
from typing import Any, Callable, List, Optional, Type

# Import with full paths to avoid naming conflicts
from fastapi import status
from fastapi.applications import FastAPI
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from uvicorn import Config, Server

from application_sdk.clients.workflow import WorkflowClient
from application_sdk.common.utils import get_workflow_config, update_workflow_config
from application_sdk.constants import (
    APP_DASHBOARD_HOST,
    APP_DASHBOARD_PORT,
    APP_HOST,
    APP_PORT,
    APP_TENANT_ID,
    APPLICATION_NAME,
    EVENT_STORE_NAME,
    WORKFLOW_UI_HOST,
    WORKFLOW_UI_PORT,
)
from application_sdk.docgen import AtlanDocsGenerator
from application_sdk.handlers import HandlerInterface
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.observability.observability import DuckDBUI
from application_sdk.server import ServerInterface
from application_sdk.server.fastapi.middleware.logmiddleware import LogMiddleware
from application_sdk.server.fastapi.middleware.metrics import MetricsMiddleware
from application_sdk.server.fastapi.models import (
    EventWorkflowRequest,
    EventWorkflowResponse,
    EventWorkflowTrigger,
    FetchMetadataRequest,
    FetchMetadataResponse,
    HttpWorkflowTrigger,
    PreflightCheckRequest,
    PreflightCheckResponse,
    TestAuthRequest,
    TestAuthResponse,
    WorkflowConfigRequest,
    WorkflowConfigResponse,
    WorkflowData,
    WorkflowRequest,
    WorkflowResponse,
    WorkflowTrigger,
)
from application_sdk.server.fastapi.routers.server import get_server_router
from application_sdk.server.fastapi.utils import internal_server_error_handler
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class APIServer(ServerInterface):
    """A FastAPI-based implementation of the ServerInterface.

    This class provides a FastAPI-based web server that handles workflow management,
    authentication, metadata operations, and event processing. It supports both HTTP and
    event-based workflow triggers.

    Attributes:
        app (FastAPI): The main FastAPI application instance.
        workflow_client (Optional[WorkflowClient]): Client for interacting with Temporal workflows.
        workflow_router (APIRouter): Router for workflow-related endpoints.
        dapr_router (APIRouter): Router for pub/sub operations.
        events_router (APIRouter): Router for event handling.
        docs_directory_path (str): Path to documentation source directory.
        docs_export_path (str): Path where documentation will be exported.
        workflows (List[WorkflowInterface]): List of registered workflows.
        event_triggers (List[EventWorkflowTrigger]): List of event-based workflow triggers.
        duckdb_ui (DuckDBUI): Instance of DuckDBUI for handling DuckDB UI functionality.

    Args:
        lifespan: Optional lifespan manager for the FastAPI application.
        handler (Optional[HandlerInterface]): Handler for processing application operations.
        workflow_client (Optional[WorkflowClient]): Client for Temporal workflow operations.
    """

    # Declare class attributes with proper typing
    app: FastAPI
    workflow_client: Optional[WorkflowClient]
    workflow_router: APIRouter
    dapr_router: APIRouter
    events_router: APIRouter
    handler: Optional[HandlerInterface]
    templates: Jinja2Templates
    duckdb_ui: DuckDBUI

    docs_directory_path: str = "docs"
    docs_export_path: str = "dist"

    workflows: List[WorkflowInterface] = []
    event_triggers: List[EventWorkflowTrigger] = []

    ui_enabled: bool = True

    def __init__(
        self,
        lifespan=None,
        handler: Optional[HandlerInterface] = None,
        workflow_client: Optional[WorkflowClient] = None,
        frontend_templates_path: str = "frontend/templates",
        ui_enabled: bool = True,
    ):
        """Initialize the FastAPI application.

        Args:
            lifespan: Optional lifespan manager for the FastAPI application.
            handler: Handler for processing application operations.
            workflow_client: Client for Temporal workflow operations.
        """
        # First, set the instance variables
        self.handler = handler
        self.workflow_client = workflow_client
        self.templates = Jinja2Templates(directory=frontend_templates_path)
        self.duckdb_ui = DuckDBUI()
        self.ui_enabled = ui_enabled

        # Create the FastAPI app using the renamed import
        if isinstance(lifespan, Callable):
            self.app = FastAPI(lifespan=lifespan)
        else:
            self.app = FastAPI()

        # Create router instances using the renamed import
        self.workflow_router = APIRouter()
        self.dapr_router = APIRouter()
        self.events_router = APIRouter()

        # Set up the application
        error_handler = internal_server_error_handler  # Store as local variable
        self.app.add_exception_handler(
            status.HTTP_500_INTERNAL_SERVER_ERROR, error_handler
        )

        # Add middleware
        self.app.add_middleware(LogMiddleware)
        self.app.add_middleware(MetricsMiddleware)

        # Register routers and setup docs
        self.register_routers()
        self.setup_atlan_docs()

        # Initialize parent class
        super().__init__(handler)

    def observability(self, request: Request) -> RedirectResponse:
        """Endpoint to launch DuckDB UI for log self-serve exploration."""
        self.duckdb_ui.start_ui()
        # Redirect to the local DuckDB UI
        return RedirectResponse(url="http://0.0.0.0:4213")

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
        self.app.include_router(self.dapr_router, prefix="/dapr")
        self.app.include_router(self.events_router, prefix="/events/v1")

    async def home(self, request: Request) -> HTMLResponse:
        return self.templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "app_dashboard_http_port": APP_DASHBOARD_PORT,
                "app_dashboard_http_host": APP_DASHBOARD_HOST,
                "app_http_port": APP_PORT,
                "app_http_host": APP_HOST,
                "tenant_id": APP_TENANT_ID,
                "app_name": APPLICATION_NAME,
                "workflow_ui_host": WORKFLOW_UI_HOST,
                "workflow_ui_port": WORKFLOW_UI_PORT,
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
        # Validate and store workflow_class at the method level to ensure it's not None
        if workflow_class is None:
            raise ValueError("workflow_class cannot be None")

        async def start_workflow_http(body: WorkflowRequest) -> WorkflowResponse:
            try:
                if not self.workflow_client:
                    raise Exception("Temporal client not initialized")

                # Use the captured wf_class variable, which is guaranteed to be non-None
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
            except Exception as e:
                logger.error(f"Error starting workflow: {e}")
                return WorkflowResponse(
                    success=False,
                    message="Workflow failed to start",
                    data=WorkflowData(
                        workflow_id="",
                        run_id="",
                    ),
                )

        # Create a closure for the start_workflow function that captures wf_class directly
        async def start_workflow_event(
            body: EventWorkflowRequest,
        ) -> EventWorkflowResponse:
            try:
                if not self.workflow_client:
                    raise Exception("Temporal client not initialized")

                # Use the captured wf_class variable, which is guaranteed to be non-None
                workflow_data = await self.workflow_client.start_workflow(
                    body.model_dump(), workflow_class=workflow_class
                )

                return EventWorkflowResponse(
                    success=True,
                    message="Workflow started successfully",
                    data=WorkflowData(
                        workflow_id=workflow_data.get("workflow_id") or "",
                        run_id=workflow_data.get("run_id") or "",
                    ),
                    status=EventWorkflowResponse.Status.SUCCESS,
                )
            except Exception as e:
                logger.error(f"Error starting workflow: {e}")
                return EventWorkflowResponse(
                    success=False,
                    message="Workflow failed to start",
                    data=WorkflowData(
                        workflow_id="",
                        run_id="",
                    ),
                    status=EventWorkflowResponse.Status.DROP,
                )

        for trigger in triggers:
            # Set the workflow class on the trigger
            trigger.workflow_class = workflow_class

            if isinstance(trigger, HttpWorkflowTrigger):
                # Add the route with our pre-defined handler
                # Getting routers as local variables to avoid module references
                self.workflow_router.add_api_route(
                    trigger.endpoint,
                    start_workflow_http,  # Use our handler with captured wf_class
                    methods=trigger.methods,
                    response_model=WorkflowResponse,
                )

                self.app.include_router(self.workflow_router, prefix="/workflows/v1")
            elif isinstance(trigger, EventWorkflowTrigger):
                self.event_triggers.append(trigger)

                self.events_router.add_api_route(
                    f"/event/{trigger.event_id}",
                    start_workflow_event,
                    methods=["POST"],
                    response_model=EventWorkflowResponse,
                )

                self.app.include_router(self.events_router, prefix="/events/v1")

    def register_routes(self):
        """
        Method to register the routes for the FastAPI application
        """

        self.app.add_api_route(
            "/observability",
            self.observability,
            methods=["GET"],
            response_class=RedirectResponse,
        )

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

        self.dapr_router.add_api_route(
            "/subscribe",
            self.get_dapr_subscriptions,
            methods=["GET"],
            response_model="list",
        )

        self.events_router.add_api_route(
            "/drop",
            self.drop_event,
            methods=["POST"],
            response_model=EventWorkflowResponse,
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

        subscriptions: List[dict[str, Any]] = []
        for event_trigger in self.event_triggers:
            filters = [
                f"({event_filter.path} {event_filter.operator} '{event_filter.value}')"
                for event_filter in event_trigger.event_filters
            ]
            filters.append(f"event.data.event_name == '{event_trigger.event_name}'")
            filters.append(f"event.data.event_type == '{event_trigger.event_type}'")

            subscriptions.append(
                {
                    "pubsubname": EVENT_STORE_NAME,
                    "topic": event_trigger.event_type + "_topic",
                    "routes": {
                        "rules": [
                            {
                                "match": " && ".join(filters),
                                "path": f"/events/v1/event/{event_trigger.event_id}",
                            }
                        ],
                        "default": "/events/v1/drop",
                    },
                }
            )

        return subscriptions

    async def drop_event(self, body: EventWorkflowRequest) -> EventWorkflowResponse:
        """Drop an event."""
        return EventWorkflowResponse(
            success=False,
            message="Event didn't match any of the filters",
            data=WorkflowData(
                workflow_id="",
                run_id="",
            ),
            status=EventWorkflowResponse.Status.DROP,
        )

    async def test_auth(self, body: TestAuthRequest) -> TestAuthResponse:
        """Test authentication credentials."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.handler:
                raise Exception("Handler not initialized")

            await self.handler.load(body.model_dump())
            await self.handler.test_auth()

            # Record successful auth
            metrics.record_metric(
                name="auth_requests_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of authentication requests",
            )

            # Record auth duration
            duration = time.time() - start_time
            metrics.record_metric(
                name="auth_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Authentication request duration in seconds",
            )

            return TestAuthResponse(success=True, message="Authentication successful")
        except Exception as e:
            # Record failed auth
            metrics.record_metric(
                name="auth_requests_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of authentication requests",
            )
            raise e

    async def fetch_metadata(self, body: FetchMetadataRequest) -> FetchMetadataResponse:
        """Fetch metadata based on request parameters."""
        start_time = time.time()
        metrics = get_metrics()

        metadata_type = body.root.get("type", "all")
        database = body.root.get("database", None)

        try:
            if not self.handler:
                raise Exception("Handler not initialized")

            await self.handler.load(body.model_dump())
            metadata = await self.handler.fetch_metadata(
                metadata_type=metadata_type, database=database
            )

            # Record successful metadata fetch
            metrics.record_metric(
                name="metadata_requests_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={
                    "status": "success",
                    "type": metadata_type,
                    "database": database,
                },
                description="Total number of metadata fetch requests",
            )

            # Record metadata fetch duration
            duration = time.time() - start_time
            metrics.record_metric(
                name="metadata_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={"type": metadata_type, "database": database},
                description="Metadata fetch duration in seconds",
            )

            return FetchMetadataResponse(success=True, data=metadata)
        except Exception as e:
            # Record failed metadata fetch
            metrics.record_metric(
                name="metadata_requests_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={
                    "status": "error",
                    "type": metadata_type,
                    "database": database,
                },
                description="Total number of metadata fetch requests",
            )
            raise e

    async def preflight_check(
        self, body: PreflightCheckRequest
    ) -> PreflightCheckResponse:
        """Perform preflight checks with provided configuration."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.handler:
                raise Exception("Handler not initialized")

            await self.handler.load(body.credentials)
            preflight_check = await self.handler.preflight_check(body.model_dump())

            # Record successful preflight check
            metrics.record_metric(
                name="preflight_checks_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of preflight checks",
            )

            # Record preflight check duration
            duration = time.time() - start_time
            metrics.record_metric(
                name="preflight_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Preflight check duration in seconds",
            )

            return PreflightCheckResponse(success=True, data=preflight_check)
        except Exception as e:
            # Record failed preflight check
            metrics.record_metric(
                name="preflight_checks_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of preflight checks",
            )
            raise e

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
        """Get the status of a specific workflow run."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            workflow_status = await self.workflow_client.get_workflow_run_status(
                workflow_id,
                run_id,
                include_last_executed_run_id=True,
            )

            # Record successful status check
            metrics.record_metric(
                name="workflow_status_checks_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of workflow status checks",
            )

            # Record status check duration
            duration = time.time() - start_time
            metrics.record_metric(
                name="workflow_status_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Workflow status check duration in seconds",
            )

            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "Workflow status fetched successfully",
                    "data": workflow_status,
                },
            )
        except Exception as e:
            # Record failed status check
            metrics.record_metric(
                name="workflow_status_checks_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of workflow status checks",
            )
            raise e

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
        """Stop a running workflow."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            await self.workflow_client.stop_workflow(workflow_id, run_id)

            # Record successful workflow stop
            metrics.record_metric(
                name="workflow_stops_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of workflow stop requests",
            )

            # Record stop duration
            duration = time.time() - start_time
            metrics.record_metric(
                name="workflow_stop_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Workflow stop duration in seconds",
            )

            return JSONResponse(
                status_code=status.HTTP_200_OK, content={"success": True}
            )
        except Exception as e:
            # Record failed workflow stop
            metrics.record_metric(
                name="workflow_stops_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of workflow stop requests",
            )
            raise e

    async def start(
        self,
        host: str = APP_HOST,
        port: int = APP_PORT,
    ) -> None:
        """Start the FastAPI application server.

        Args:
            host (str, optional): Host address to bind to. Defaults to "0.0.0.0".
            port (int, optional): Port to listen on. Defaults to 8000.
        """
        if self.ui_enabled:
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
