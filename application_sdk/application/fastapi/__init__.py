import logging
from typing import Any, Callable, List, Optional, Type

from fastapi import APIRouter, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from uvicorn import Config, Server

from application_sdk.application import AtlanApplicationInterface
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
from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.common.utils import get_workflow_config, update_workflow_config
from application_sdk.handlers import HandlerInterface
from application_sdk.outputs.eventstore import AtlanEvent, EventStore
from application_sdk.workflows import WorkflowInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class WorkflowTrigger(BaseModel):
    workflow_class: Optional[Type[WorkflowInterface]] = None
    model_config = {"arbitrary_types_allowed": True}


class HttpWorkflowTrigger(WorkflowTrigger):
    endpoint: str
    methods: List[str]


class EventWorkflowTrigger(WorkflowTrigger):
    should_trigger_workflow: Callable[[Any], bool]


class FastAPIApplication(AtlanApplicationInterface):
    """
    Class for Fast API Application
    """

    app: FastAPI
    temporal_client: Optional[TemporalClient]

    workflow_router: APIRouter = APIRouter()
    dapr_router: APIRouter = APIRouter()
    events_router: APIRouter = APIRouter()

    workflows: List[WorkflowInterface] = []
    event_triggers: List[EventWorkflowTrigger] = []

    def __init__(
        self,
        lifespan=None,
        handler: Optional[HandlerInterface] = None,
        temporal_client: Optional[TemporalClient] = None,
    ):
        self.app = FastAPI(lifespan=lifespan)
        self.app.add_exception_handler(
            status.HTTP_500_INTERNAL_SERVER_ERROR, internal_server_error_handler
        )
        self.handler = handler
        self.temporal_client = temporal_client

        self.register_routers()
        super().__init__(handler)

    def register_routers(self):
        # Register all routes first
        self.register_routes()

        # Then include all routers
        self.app.include_router(get_server_router())
        self.app.include_router(self.workflow_router, prefix="/workflows/v1")
        self.app.include_router(self.dapr_router, prefix="/dapr")
        self.app.include_router(self.events_router, prefix="/events/v1")

    def register_workflow(
        self, workflow_class: Type[WorkflowInterface], triggers: List[WorkflowTrigger]
    ):
        for trigger in triggers:
            trigger.workflow_class = workflow_class

            if isinstance(trigger, HttpWorkflowTrigger):

                async def start_workflow(body: WorkflowRequest):
                    if not self.temporal_client:
                        raise Exception("Temporal client not initialized")

                    workflow_data = await self.temporal_client.start_workflow(
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

        self.dapr_router.add_api_route(
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

    async def get_dapr_subscriptions(
        self,
    ) -> List[dict[str, Any]]:
        return [
            {
                "pubsubname": EventStore.EVENT_STORE_NAME,
                "topic": EventStore.TOPIC_NAME,
                "routes": {"rules": [{"path": "events/v1/event"}]},
            }
        ]

    async def on_event(self, event: dict[str, Any]):
        if not self.temporal_client:
            raise Exception("Temporal client not initialized")

        logger.info(f"Received event {event}")
        for trigger in self.event_triggers:
            if trigger.should_trigger_workflow(AtlanEvent(**event)):
                logger.info(
                    f"Triggering workflow {trigger.workflow_class} with event {event}"
                )

                await self.temporal_client.start_workflow(
                    workflow_args=event, workflow_class=trigger.workflow_class
                )

    async def test_auth(self, body: TestAuthRequest) -> TestAuthResponse:
        """
        Get the credentials from the request body and test the authentication
        """
        await self.handler.load(body.model_dump())
        await self.handler.test_auth()
        return TestAuthResponse(success=True, message="Authentication successful")

    async def fetch_metadata(self, body: FetchMetadataRequest) -> FetchMetadataResponse:
        """
        Get the credentials from the request body and fetch the metadata
        """
        await self.handler.load(body.model_dump())
        metadata = await self.handler.fetch_metadata(
            metadata_type=body.root["type"], database=body.root["database"]
        )
        return FetchMetadataResponse(success=True, data=metadata)

    async def preflight_check(
        self, body: PreflightCheckRequest
    ) -> PreflightCheckResponse:
        """
        Get the credentials from the request body and perform preflight checks
        """
        preflight_check = await self.handler.preflight_check(body.model_dump())
        return PreflightCheckResponse(success=True, data=preflight_check)

    def get_workflow_config(self, config_id: str) -> WorkflowConfigResponse:
        config = get_workflow_config(config_id)
        return WorkflowConfigResponse(
            success=True,
            message="Workflow configuration fetched successfully",
            data=config,
        )

    async def get_workflow_run_status(
        self, workflow_id: str, run_id: str
    ) -> JSONResponse:
        """
        Get the status of a workflow run
        Args:
            workflow_id: The ID of the workflow
            run_id: The ID of the run (optional, if not provided, the status of the current or last run will be returned)
        Returns:
            JSONResponse containing the status of the workflow
        """
        if not self.temporal_client:
            raise Exception("Temporal client not initialized")

        workflow_status = await self.temporal_client.get_workflow_run_status(
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
        # note: it's assumed that the preflight check is successful if the config is being updated
        config = update_workflow_config(config_id, body.model_dump())
        return WorkflowConfigResponse(
            success=True,
            message="Workflow configuration updated successfully",
            data=config,
        )

    async def start(self, host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
        """Start the FastAPI application with the given host and port.
        
        Args:
            host: The host to bind to
            port: The port to bind to
            reload: Whether to enable hot-reloading (only for development)
        """
        config = Config(
            app=self.app,
            host=host,
            port=port,
            reload=reload,
            reload_includes=["*.py"],  # Watch Python files for changes
            reload_dirs=["."],  # Watch current directory
        )
        
        server = Server(config)
        await server.serve()
