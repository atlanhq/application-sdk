from fastapi import APIRouter, FastAPI

from application_sdk.app.rest import AtlanApplication
from application_sdk.app.rest.fastapi.dto.workflow import (
    FetchMetadataRequest,
    FetchMetadataResponse,
    PreflightCheckRequest,
    PreflightCheckResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
    TestAuthRequest,
    TestAuthResponse,
)
from application_sdk.app.rest.fastapi.middlewares.http_controller import http_controller
from application_sdk.app.rest.fastapi.middlewares.requires import requires
from application_sdk.app.rest.fastapi.middlewares.validation import validation
from application_sdk.app.rest.fastapi.routers.health import get_health_router
from application_sdk.app.rest.fastapi.routers.logs import get_logs_router
from application_sdk.app.rest.fastapi.routers.metrics import get_metrics_router
from application_sdk.app.rest.fastapi.routers.traces import get_traces_router
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.workflow import WorkflowInterface


class FastAPIApplication(AtlanApplication):
    app: FastAPI

    workflow_router: APIRouter = APIRouter()

    workflow: WorkflowInterface | None = None

    def __init__(
        self,
        auth_controller: WorkflowAuthControllerInterface | None = None,
        metadata_controller: WorkflowMetadataControllerInterface | None = None,
        preflight_check_controller: WorkflowPreflightCheckControllerInterface
        | None = None,
        workflow: WorkflowInterface | None = None,
        *args,
        **kwargs,
    ):
        self.app = FastAPI()

        self.auth_controller = auth_controller
        self.metadata_controller = metadata_controller
        self.preflight_check_controller = preflight_check_controller

        self.workflow = workflow

        super().__init__(
            auth_controller,
            metadata_controller,
            preflight_check_controller,
            *args,
            **kwargs,
        )

    def register_routers(self):
        self.app.include_router(get_health_router(), prefix="/health")
        self.app.include_router(get_logs_router(), prefix="/logs")
        self.app.include_router(get_metrics_router(), prefix="/metrics")
        self.app.include_router(get_traces_router(), prefix="/traces")

        self.app.include_router(self.workflow_router, prefix="/workflows/v1")

        super().register_routers()

    def register_routes(self):
        self.workflow_router.add_api_route(
            "/test_auth",
            self.test_auth,
            methods=["POST"],
            response_model=TestAuthResponse,
        )
        self.workflow_router.add_api_route(
            "/fetch_metadata",
            self.fetch_metadata,
            methods=["POST"],
            response_model=FetchMetadataResponse,
        )
        self.workflow_router.add_api_route(
            "/preflight_check",
            self.preflight_check,
            methods=["POST"],
            response_model=PreflightCheckResponse,
        )
        self.workflow_router.add_api_route(
            "/start_workflow",
            self.start_workflow,
            methods=["POST"],
            response_model=StartWorkflowResponse,
        )

        super().register_routes()

    @http_controller
    @validation(TestAuthRequest)
    @requires("auth_controller")
    async def test_auth(self, body: TestAuthRequest, **_) -> TestAuthResponse:
        await self.auth_controller.test_auth(body.credential)
        return TestAuthResponse(success=True, message="Authentication successful")

    @http_controller
    @validation(FetchMetadataRequest)
    @requires("metadata_controller")
    async def fetch_metadata(
        self, request: FetchMetadataRequest
    ) -> FetchMetadataResponse:
        metadata = await self.metadata_controller.fetch_metadata(request.credential)
        return FetchMetadataResponse(success=True, metadata=metadata)

    @http_controller
    @validation(PreflightCheckRequest)
    @requires("preflight_check_controller")
    async def preflight_check(
        self, request: PreflightCheckRequest
    ) -> PreflightCheckResponse:
        preflight_check = await self.preflight_check_controller.preflight_check(
            request.form_data
        )
        return PreflightCheckResponse(success=True, preflight_check=preflight_check)

    @http_controller
    @validation(StartWorkflowRequest)
    @requires("workflow")
    async def start_workflow(
        self, request: StartWorkflowRequest
    ) -> StartWorkflowResponse:
        workflow_metadata = await self.workflow.start(
            request.input, workflow_class=self.workflow.__class__
        )
        return StartWorkflowResponse(success=True, workflow_metadata=workflow_metadata)
