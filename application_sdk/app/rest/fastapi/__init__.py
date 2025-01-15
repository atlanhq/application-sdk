import logging
from typing import Any, Callable, List, Optional

from fastapi import APIRouter, FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from application_sdk.app.rest import AtlanAPIApplication, AtlanAPIApplicationConfig
from application_sdk.app.rest.fastapi.middlewares.error_handler import (
    internal_server_error_handler,
)
from application_sdk.app.rest.fastapi.models.workflow import (
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
from application_sdk.app.rest.fastapi.routers.logs import get_logs_router
from application_sdk.app.rest.fastapi.routers.metrics import get_metrics_router
from application_sdk.app.rest.fastapi.routers.system import get_health_router
from application_sdk.app.rest.fastapi.routers.traces import get_traces_router
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.paas.eventstore import EventStore
from application_sdk.paas.eventstore.models import AtlanEvent
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.resources.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)
from application_sdk.workflows.workflow import WorkflowInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class FastAPIApplicationConfig(AtlanAPIApplicationConfig):
    lifespan = None

    def __init__(self, lifespan=None, *args, **kwargs):
        self.lifespan = lifespan
        super().__init__(*args, **kwargs)


class WorkflowTrigger(BaseModel):
    workflow: Optional[WorkflowInterface] = None
    model_config = {"arbitrary_types_allowed": True}


class HttpWorkflowTrigger(WorkflowTrigger):
    endpoint: str
    methods: List[str]


class EventWorkflowTrigger(WorkflowTrigger):
    should_trigger_workflow: Callable[[Any], bool]


class FastAPIApplication(AtlanAPIApplication):
    app: FastAPI

    workflow_router: APIRouter = APIRouter()
    dapr_router: APIRouter = APIRouter()
    events_router: APIRouter = APIRouter()
    system_router: APIRouter = APIRouter()

    workflows: List[WorkflowInterface] = []
    event_triggers: List[EventWorkflowTrigger] = []

    def __init__(
        self,
        auth_controller: WorkflowAuthControllerInterface | None = None,
        metadata_controller: WorkflowMetadataControllerInterface | None = None,
        preflight_check_controller: WorkflowPreflightCheckControllerInterface
        | None = None,
        config: FastAPIApplicationConfig = FastAPIApplicationConfig(),
        *args,
        **kwargs,
    ):
        self.app = FastAPI(lifespan=config.lifespan if config else None)
        self.app.add_exception_handler(
            status.HTTP_500_INTERNAL_SERVER_ERROR, internal_server_error_handler
        )

        self.auth_controller = auth_controller
        self.metadata_controller = metadata_controller
        self.preflight_check_controller = preflight_check_controller

        super().__init__(
            auth_controller,
            metadata_controller,
            preflight_check_controller,
            config,
            *args,
            **kwargs,
        )

    def register_routers(self):
        self.app.include_router(get_health_router())
        self.app.include_router(get_logs_router())
        self.app.include_router(get_metrics_router())
        self.app.include_router(get_traces_router())

        self.app.include_router(self.workflow_router, prefix="/workflows/v1")
        self.app.include_router(self.dapr_router, prefix="/dapr")
        self.app.include_router(self.events_router, prefix="/events/v1")
        self.app.include_router(self.system_router, prefix="/system")

        super().register_routers()

    def register_workflow(
        self, workflow: WorkflowInterface, triggers: List[WorkflowTrigger]
    ):
        for trigger in triggers:
            trigger.workflow = workflow

            if isinstance(trigger, HttpWorkflowTrigger):

                async def start_workflow(body: WorkflowRequest):
                    workflow_data = await workflow.start(
                        body.model_dump(), workflow_class=workflow.__class__
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
            "/status/{workflow_id}/{run_id}",
            self.get_workflow_status,
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

        super().register_routes()

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
        logger.info(f"Received event {event}")
        for trigger in self.event_triggers:
            if trigger.should_trigger_workflow(AtlanEvent(**event)):
                logger.info(
                    f"Triggering workflow {trigger.workflow} with event {event}"
                )

                await trigger.workflow.start(
                    workflow_args=event, workflow_class=trigger.workflow.__class__
                )

    async def test_auth(self, body: TestAuthRequest) -> TestAuthResponse:
        await self.auth_controller.prepare(body.model_dump())
        await self.auth_controller.test_auth()
        return TestAuthResponse(success=True, message="Authentication successful")

    async def fetch_metadata(self, body: FetchMetadataRequest) -> FetchMetadataResponse:
        """
        Fetch metadata based on the requested type.
        Args:
            body: Request body containing optional type and database parameters
        Returns:
            FetchMetadataResponse containing the requested metadata
        """
        await self.metadata_controller.prepare(body.model_dump())
        metadata = await self.metadata_controller.fetch_metadata(
            metadata_type=body.root["type"], database=body.root["database"]
        )
        return FetchMetadataResponse(success=True, data=metadata)

    async def preflight_check(
        self, body: PreflightCheckRequest
    ) -> PreflightCheckResponse:
        preflight_check = await self.preflight_check_controller.preflight_check(
            body.model_dump()
        )
        return PreflightCheckResponse(success=True, data=preflight_check)

    def get_workflow_config(self, config_id: str) -> WorkflowConfigResponse:
        config = self.metadata_controller.get_workflow_config(config_id)
        return WorkflowConfigResponse(
            success=True,
            message="Workflow configuration fetched successfully",
            data=config,
        )

    async def get_workflow_status(self, workflow_id: str, run_id: str) -> JSONResponse:
        """
        Get the status of a workflow
        Args:
            workflow_id: The ID of the workflow
            run_id: The ID of the run
        Returns:
            JSONResponse containing the status of the workflow
        """
        temporal_resource = TemporalResource(TemporalConfig())
        await temporal_resource.load()
        workflow_status = await temporal_resource.get_workflow_status(
            workflow_id, run_id
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
        config = self.metadata_controller.update_workflow_config(
            config_id, body.model_dump()
        )
        return WorkflowConfigResponse(
            success=True,
            message="Workflow configuration updated successfully",
            data=config,
        )
