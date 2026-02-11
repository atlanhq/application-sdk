import os
import time
from typing import Any, Callable, Dict, List, Optional, Type

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
    AddScheduleRequest,
    ConfigMapResponse,
    DeleteScheduleResponse,
    EditScheduleRequest,
    EventWorkflowRequest,
    EventWorkflowResponse,
    EventWorkflowTrigger,
    FetchMetadataRequest,
    FetchMetadataResponse,
    HttpWorkflowTrigger,
    ListSchedulesResponse,
    PreflightCheckRequest,
    PreflightCheckResponse,
    ScheduleData,
    ScheduleDetailsData,
    ScheduleDetailsResponse,
    ScheduleListItem,
    ScheduleResponse,
    Subscription,
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
from application_sdk.services.statestore import StateStore, StateType
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
    subscription_router: APIRouter
    handler: Optional[HandlerInterface]
    templates: Jinja2Templates
    duckdb_ui: DuckDBUI

    docs_directory_path: str = "docs"
    docs_export_path: str = "dist"
    # List of subscriptions to be registered
    subscriptions: List[Subscription] = []

    frontend_assets_path: str = "frontend/static"

    workflows: List[WorkflowInterface] = []
    event_triggers: List[EventWorkflowTrigger] = []
    _workflow_classes: Dict[str, Type[WorkflowInterface]] = {}

    ui_enabled: bool = True

    def __init__(
        self,
        lifespan=None,
        handler: Optional[HandlerInterface] = None,
        workflow_client: Optional[WorkflowClient] = None,
        frontend_templates_path: str = "frontend/templates",
        ui_enabled: bool = True,
        has_configmap: bool = False,
        subscriptions: List[Subscription] = [],
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
        self.has_configmap = has_configmap

        # Create the FastAPI app using the renamed import
        if isinstance(lifespan, Callable):
            self.app = FastAPI(lifespan=lifespan)
        else:
            self.app = FastAPI()

        # Create router instances using the renamed import
        self.workflow_router = APIRouter()
        self.dapr_router = APIRouter()
        self.events_router = APIRouter()
        self.subscriptions = subscriptions
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

    def frontend_home(self, request: Request) -> HTMLResponse:
        frontend_html_path = os.path.join(
            self.frontend_assets_path,
            "index.html",
        )

        if not os.path.exists(frontend_html_path) or not self.has_configmap:
            return self.fallback_home(request)

        with open(frontend_html_path, "r", encoding="utf-8") as file:
            contents = file.read()

        return HTMLResponse(content=contents)

    def register_routers(self):
        """Register all routers with the FastAPI application.

        Registers routes and includes all routers with their respective prefixes:
        - Server router
        - Workflow router (/workflows/v1)
        - Pubsub router (/dapr)
        - Events router (/events/v1)
        - Subscription router (/subscriptions/v1)
        """
        # Register all routes first
        self.register_routes()

        # Then include all routers
        self.app.include_router(get_server_router())
        self.app.include_router(self.workflow_router, prefix="/workflows/v1")
        self.app.include_router(self.dapr_router, prefix="/dapr")
        self.app.include_router(self.events_router, prefix="/events/v1")

        # Register subscription routes from subscriptions with handler callbacks
        subscription_router = APIRouter()
        for subscription in self.subscriptions:
            subscription_router.add_api_route(
                f"/{subscription.route}",
                subscription.handler,
                methods=["POST"],
            )
        self.app.include_router(subscription_router, prefix="/subscriptions/v1")

    def fallback_home(self, request: Request) -> HTMLResponse:
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

        # Register workflow class for schedule endpoint resolution
        self._workflow_classes[workflow_class.__name__] = workflow_class

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

        self.workflow_router.add_api_route(
            "/pause/{workflow_id}/{run_id:path}",
            self.pause_workflow,
            methods=["POST"],
        )

        self.workflow_router.add_api_route(
            "/resume/{workflow_id}/{run_id:path}",
            self.resume_workflow,
            methods=["POST"],
        )

        self.workflow_router.add_api_route(
            "/configmap/{config_map_id}",
            self.get_configmap,
            methods=["GET"],
            response_model=ConfigMapResponse,
        )

        self.workflow_router.add_api_route(
            "/schedule",
            self.add_schedule,
            methods=["POST"],
            response_model=ScheduleResponse,
        )
        self.workflow_router.add_api_route(
            "/schedule",
            self.list_schedules,
            methods=["GET"],
            response_model=ListSchedulesResponse,
        )
        self.workflow_router.add_api_route(
            "/schedule/{schedule_id}",
            self.get_schedule_details,
            methods=["GET"],
            response_model=ScheduleDetailsResponse,
        )
        self.workflow_router.add_api_route(
            "/schedule/{schedule_id}",
            self.edit_schedule,
            methods=["PUT"],
            response_model=ScheduleResponse,
        )
        self.workflow_router.add_api_route(
            "/schedule/{schedule_id}",
            self.delete_schedule,
            methods=["DELETE"],
            response_model=DeleteScheduleResponse,
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
        self.app.get("/")(self.frontend_home)

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
        for subscription in self.subscriptions:
            subscription_dict: dict[str, Any] = {
                "pubsubname": subscription.component_name,
                "topic": subscription.topic,
                "route": f"/subscriptions/v1/{subscription.route}",
            }
            if subscription.bulk_config:
                subscription_dict["bulkSubscribe"] = (
                    subscription.bulk_config.model_dump(by_alias=True)
                )
            if subscription.dead_letter_topic:
                subscription_dict["deadLetterTopic"] = subscription.dead_letter_topic
            subscriptions.append(subscription_dict)
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
                    "topic": event_trigger.event_type,
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
        database = body.root.get("database", "")

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

    async def get_configmap(self, config_map_id: str) -> ConfigMapResponse:
        """Get a configuration map by its ID.

        Args:
            config_map_id (str): The ID of the configuration map to retrieve.

        Returns:
            ConfigMapResponse: Response containing the configuration map.
        """
        try:
            if not self.handler:
                raise Exception("Handler not initialized")

            # Call the getConfigmap method on the workflow class
            config_map_data = await self.handler.get_configmap(config_map_id)

            return ConfigMapResponse(
                success=True,
                message="Configuration map fetched successfully",
                data=config_map_data,
            )
        except Exception as e:
            logger.error(f"Error fetching configuration map: {e}")
            return ConfigMapResponse(
                success=False,
                message=f"Failed to fetch configuration map: {str(e)}",
                data={},
            )

    async def get_workflow_config(
        self, config_id: str, type: str = "workflows"
    ) -> WorkflowConfigResponse:
        """Retrieve workflow configuration by ID.

        Args:
            config_id (str): The ID of the configuration to retrieve.
            type (str): The type of the configuration to retrieve.

        Returns:
            WorkflowConfigResponse: Response containing the workflow configuration.
        """
        if not StateType.is_member(type):
            raise ValueError(f"Invalid type {type} for state store")

        config = await StateStore.get_state(config_id, StateType(type))
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

    async def update_workflow_config(
        self, config_id: str, body: WorkflowConfigRequest, type: str = "workflows"
    ) -> WorkflowConfigResponse:
        """Update workflow configuration.

        Args:
            config_id (str): The ID of the workflow configuration to update.
            body (WorkflowConfigRequest): The new configuration data.

        Returns:
            WorkflowConfigResponse: Response containing the updated configuration.
        """
        if not StateType.is_member(type):
            raise ValueError(f"Invalid type {type} for state store")

        config = await StateStore.save_state_object(
            id=config_id, value=body.model_dump(), type=StateType(type)
        )
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

    async def pause_workflow(self, workflow_id: str, run_id: str) -> JSONResponse:
        """Pause a running workflow."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            await self.workflow_client.pause_workflow(workflow_id, run_id)

            metrics.record_metric(
                name="workflow_pauses_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of workflow pause requests",
            )

            duration = time.time() - start_time
            metrics.record_metric(
                name="workflow_pause_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Workflow pause duration in seconds",
            )

            return JSONResponse(
                status_code=status.HTTP_200_OK, content={"success": True}
            )
        except Exception as e:
            metrics.record_metric(
                name="workflow_pauses_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of workflow pause requests",
            )
            raise e

    async def resume_workflow(self, workflow_id: str, run_id: str) -> JSONResponse:
        """Resume a paused workflow."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            await self.workflow_client.resume_workflow(workflow_id, run_id)

            metrics.record_metric(
                name="workflow_resumes_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of workflow resume requests",
            )

            duration = time.time() - start_time
            metrics.record_metric(
                name="workflow_resume_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Workflow resume duration in seconds",
            )

            return JSONResponse(
                status_code=status.HTTP_200_OK, content={"success": True}
            )
        except Exception as e:
            metrics.record_metric(
                name="workflow_resumes_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of workflow resume requests",
            )
            raise e

    def _resolve_workflow_class(
        self, name: Optional[str] = None
    ) -> Type[WorkflowInterface]:
        """Resolve a workflow class by name or return the first registered one.

        Args:
            name: Optional workflow class name. If None, returns the first registered class.

        Returns:
            The resolved workflow class.

        Raises:
            ValueError: If no workflow classes are registered or name is not found.
        """
        if not self._workflow_classes:
            raise ValueError(
                "No workflow classes registered. Call register_workflow() first."
            )
        if name:
            if name not in self._workflow_classes:
                raise ValueError(
                    f"Workflow class '{name}' not found. "
                    f"Available: {list(self._workflow_classes.keys())}"
                )
            return self._workflow_classes[name]
        return next(iter(self._workflow_classes.values()))

    async def add_schedule(self, body: AddScheduleRequest) -> ScheduleResponse:
        """Create a new workflow schedule."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            workflow_class = self._resolve_workflow_class(body.workflow_class_name)
            schedule_data = await self.workflow_client.create_schedule(
                schedule_id=body.schedule_id,
                schedule_args=body.model_dump(),
                workflow_class=workflow_class,
            )

            metrics.record_metric(
                name="schedule_creates_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of schedule create requests",
            )

            duration = time.time() - start_time
            metrics.record_metric(
                name="schedule_create_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Schedule create duration in seconds",
            )

            return ScheduleResponse(
                success=True,
                message="Schedule created successfully",
                data=ScheduleData(
                    schedule_id=schedule_data.get("schedule_id", body.schedule_id),
                ),
            )
        except Exception as e:
            metrics.record_metric(
                name="schedule_creates_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of schedule create requests",
            )
            raise e

    async def list_schedules(self) -> ListSchedulesResponse:
        """List all workflow schedules."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            schedules = await self.workflow_client.list_schedules()

            metrics.record_metric(
                name="schedule_lists_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of schedule list requests",
            )

            duration = time.time() - start_time
            metrics.record_metric(
                name="schedule_list_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Schedule list duration in seconds",
            )

            return ListSchedulesResponse(
                success=True,
                message="Schedules fetched successfully",
                data=[
                    ScheduleListItem(
                        schedule_id=s["schedule_id"],
                        paused=s["paused"],
                        note=s.get("note"),
                        cron_expression=s.get("cron_expression"),
                    )
                    for s in schedules
                ],
            )
        except Exception as e:
            metrics.record_metric(
                name="schedule_lists_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of schedule list requests",
            )
            raise e

    async def get_schedule_details(self, schedule_id: str) -> ScheduleDetailsResponse:
        """Get details of a workflow schedule."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            schedule_info = await self.workflow_client.get_schedule(schedule_id)

            metrics.record_metric(
                name="schedule_gets_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of schedule get requests",
            )

            duration = time.time() - start_time
            metrics.record_metric(
                name="schedule_get_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Schedule get duration in seconds",
            )

            return ScheduleDetailsResponse(
                success=True,
                message="Schedule fetched successfully",
                data=ScheduleDetailsData(
                    schedule_id=schedule_info["schedule_id"],
                    cron_expression=schedule_info["cron_expression"],
                    paused=schedule_info["paused"],
                    note=schedule_info.get("note"),
                    workflow_args=schedule_info.get("workflow_args", {}),
                    recent_actions=schedule_info.get("recent_actions", []),
                    next_action_times=schedule_info.get("next_action_times", []),
                ),
            )
        except Exception as e:
            metrics.record_metric(
                name="schedule_gets_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of schedule get requests",
            )
            raise e

    async def edit_schedule(
        self, schedule_id: str, body: EditScheduleRequest
    ) -> ScheduleResponse:
        """Update an existing workflow schedule."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            await self.workflow_client.update_schedule(
                schedule_id=schedule_id,
                schedule_args=body.model_dump(exclude_none=True),
            )

            metrics.record_metric(
                name="schedule_updates_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of schedule update requests",
            )

            duration = time.time() - start_time
            metrics.record_metric(
                name="schedule_update_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Schedule update duration in seconds",
            )

            return ScheduleResponse(
                success=True,
                message="Schedule updated successfully",
                data=ScheduleData(schedule_id=schedule_id),
            )
        except Exception as e:
            metrics.record_metric(
                name="schedule_updates_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of schedule update requests",
            )
            raise e

    async def delete_schedule(self, schedule_id: str) -> DeleteScheduleResponse:
        """Delete a workflow schedule."""
        start_time = time.time()
        metrics = get_metrics()

        try:
            if not self.workflow_client:
                raise Exception("Temporal client not initialized")

            await self.workflow_client.delete_schedule(schedule_id)

            metrics.record_metric(
                name="schedule_deletes_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "success"},
                description="Total number of schedule delete requests",
            )

            duration = time.time() - start_time
            metrics.record_metric(
                name="schedule_delete_duration_seconds",
                value=duration,
                metric_type=MetricType.HISTOGRAM,
                labels={},
                description="Schedule delete duration in seconds",
            )

            return DeleteScheduleResponse(
                success=True,
                message="Schedule deleted successfully",
            )
        except Exception as e:
            metrics.record_metric(
                name="schedule_deletes_total",
                value=1.0,
                metric_type=MetricType.COUNTER,
                labels={"status": "error"},
                description="Total number of schedule delete requests",
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
