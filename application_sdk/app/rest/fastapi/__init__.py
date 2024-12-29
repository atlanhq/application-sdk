from typing import Any, Callable, List

from fastapi import APIRouter, FastAPI, status

from application_sdk.app.rest import AtlanAPIApplication, AtlanAPIApplicationConfig
from application_sdk.app.rest.fastapi.middlewares.error_handler import (
    internal_server_error_handler,
)
from application_sdk.app.rest.fastapi.models.workflow import (
    FetchMetadataRequest,
    FetchMetadataResponse,
    PreflightCheckRequest,
    PreflightCheckResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
    TestAuthRequest,
    TestAuthResponse,
    WorkflowData,
)
from application_sdk.app.rest.fastapi.routers.health import get_health_router
from application_sdk.app.rest.fastapi.routers.logs import get_logs_router
from application_sdk.app.rest.fastapi.routers.metrics import get_metrics_router
from application_sdk.app.rest.fastapi.routers.traces import get_traces_router
from application_sdk.paas.eventstore import EventStore
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.workflow import WorkflowInterface
from application_sdk.paas.eventstore.models import DaprEvent

class FastAPIApplicationConfig(AtlanAPIApplicationConfig):
    lifespan = None

    def __init__(self, lifespan=None, *args, **kwargs):
        self.lifespan = lifespan
        super().__init__(*args, **kwargs)


class FastAPIApplication(AtlanAPIApplication):
    app: FastAPI

    workflow_router: APIRouter = APIRouter()
    dapr_router: APIRouter = APIRouter()
    events_router: APIRouter = APIRouter()

    workflow: WorkflowInterface | None = None

    subscriptions: dict[str, Any] = {}

    def __init__(
        self,
        auth_controller: WorkflowAuthControllerInterface | None = None,
        metadata_controller: WorkflowMetadataControllerInterface | None = None,
        preflight_check_controller: WorkflowPreflightCheckControllerInterface
        | None = None,
        workflow: WorkflowInterface | None = None,
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

        self.workflow = workflow

        self.subscriptions: dict[str, Any] = {}
        self.workflow_triggers: List[dict[str, Any]] = []

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

        super().register_routers()

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
            "/start",
            self.start_workflow,
            methods=["POST"],
            response_model=StartWorkflowResponse,
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

    def register_event_trigger(
        self,
        workflow: type[WorkflowInterface],
        should_trigger_workflow: Callable[[Any], bool],
    ) -> List[dict[str, Any]]:
        self.workflow_triggers.append(
            {"workflow": workflow, "should_trigger_workflow": should_trigger_workflow}
        )
        return [trigger["workflow"].__name__ for trigger in self.workflow_triggers]

    async def get_dapr_subscriptions(
        self,
    ) -> List[dict[str, Any]]:
        return [
            {
                "pubsubname": "eventstore",
                "topic": EventStore.TOPIC_NAME,
                "routes": {"rules": [{"path": f"events/v1/event"}]},
            }
        ]

    async def on_event(self, event: dict[str, Any]):
        print(f"Received event {event}")
        print(f"Workflow triggers: {len(self.workflow_triggers)}")
        for trigger in self.workflow_triggers:
            if trigger["should_trigger_workflow"](DaprEvent(**event)):
                print(f"Triggering workflow {trigger['workflow']} with event {event}")

                await trigger["workflow"]().start(
                    workflow_args=event, workflow_class=trigger["workflow"].__class__
                )

    async def test_auth(self, body: TestAuthRequest) -> TestAuthResponse:
        await self.auth_controller.prepare(body.model_dump())
        await self.auth_controller.test_auth()
        return TestAuthResponse(success=True, message="Authentication successful")

    async def fetch_metadata(self, body: FetchMetadataRequest) -> FetchMetadataResponse:
        await self.metadata_controller.prepare(body.model_dump())
        metadata = await self.metadata_controller.fetch_metadata()
        return FetchMetadataResponse(success=True, data=metadata)

    async def preflight_check(
        self, body: PreflightCheckRequest
    ) -> PreflightCheckResponse:
        preflight_check = await self.preflight_check_controller.preflight_check(
            body.model_dump()
        )
        return PreflightCheckResponse(success=True, data=preflight_check)

    async def start_workflow(self, body: StartWorkflowRequest) -> StartWorkflowResponse:
        workflow_data = await self.workflow.start(
            body.model_dump(), workflow_class=self.workflow.__class__
        )
        return StartWorkflowResponse(
            success=True,
            message="Workflow started successfully",
            data=WorkflowData(
                workflow_id=workflow_data["workflow_id"],
                run_id=workflow_data["run_id"],
            ),
        )
