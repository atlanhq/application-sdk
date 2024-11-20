from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

from application_sdk.app import AtlanApplicationBuilder
from application_sdk.app.rest.fastapi.routers import health, logs, metrics, traces
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.resources.temporal_resource import ResourceInterface
from application_sdk.workflows.workflow import WorkflowInterface


class FastAPIApplicationBuilder(AtlanApplicationBuilder):
    auth_controller: WorkflowAuthControllerInterface | None
    metadata_controller: WorkflowMetadataControllerInterface | None
    preflight_check_controller: WorkflowPreflightCheckControllerInterface | None
    resource: ResourceInterface | None
    workflow: WorkflowInterface

    workflows_router: APIRouter = APIRouter(
        prefix="/workflows/v1",
        tags=["workflows"],
        responses={404: {"description": "Not found"}},
    )

    def __init__(
        self,
        app: FastAPI,
        workflow: WorkflowInterface,
        resource: Optional[ResourceInterface] | None = None,
        auth_controller: Optional[WorkflowAuthControllerInterface] | None = None,
        metadata_controller: Optional[WorkflowMetadataControllerInterface]
        | None = None,
        preflight_check_controller: Optional[WorkflowPreflightCheckControllerInterface]
        | None = None,
    ):
        self.app = app
        self.app.include_router(health.router)
        self.auth_controller = auth_controller
        self.metadata_controller = metadata_controller
        self.preflight_check_controller = preflight_check_controller
        self.resource = resource
        self.workflow = workflow

        super().__init__(
            auth_controller=auth_controller,
            metadata_controller=metadata_controller,
            preflight_check_controller=preflight_check_controller,
        )

    def add_telemetry_routes(self) -> None:
        self.app.include_router(logs.router)
        self.app.include_router(metrics.router)
        self.app.include_router(traces.router)

    def on_api_service_start(self):
        super().on_api_service_start()
        # FastAPIInstrumentor.instrument_app(self.app)  # pyright: ignore[reportUnknownMemberType]

    async def test_auth(self, credential: Dict[str, Any]):
        if not self.auth_controller:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Auth interface not implemented",
            )
        try:
            self.auth_controller.sql_resource.set_credentials(credential)
            await self.auth_controller.sql_resource.load()
            await self.auth_controller.test_auth()
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "Authentication successful",
                },
            )
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "success": False,
                    "message": "Failed to test authentication",
                    "error": str(e),
                },
            )

    async def fetch_metadata(self, credential: Dict[str, Any]):
        if not self.metadata_controller:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Metadata interface not implemented",
            )

        if not self.resource:
            raise HTTPException(status_code=500, detail="Resource not implemented")
        try:
            self.resource.set_credentials(credential)
            await self.resource.load()

            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "data": await self.metadata_controller.fetch_metadata(),
                },
            )
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "success": False,
                    "message": "Failed to fetch metadata",
                    "error": str(e),
                },
            )

    async def preflight_check(self, form_data: Dict[str, Any]):
        if not self.preflight_check_controller:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Preflight check controller not implemented",
            )

        if not self.resource:
            raise HTTPException(status_code=500, detail="Resource not implemented")
        try:
            if not form_data["credentials"]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Credentials not passed",
                )

            self.resource.set_credentials(form_data["credentials"])
            await self.resource.load()
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "data": await self.preflight_check_controller.preflight_check(
                        form_data
                    ),
                },
            )
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "success": False,
                    "message": "Preflight check failed",
                    "error": str(e),
                },
            )

    async def start_workflow(self, workflow_args: Dict[str, Any]):
        if not self.workflow:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Workflow interface not implemented",
            )
        try:
            workflow_metadata = await self.workflow.start(
                workflow_args, workflow_class=self.workflow.__class__
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "Workflow started successfully",
                    "data": workflow_metadata,
                },
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
            )

    def add_workflows_router(self):
        self.workflows_router.add_api_route(
            path="/auth",
            endpoint=self.test_auth,
            methods=["POST"],
            response_model=bool,
        )

        self.workflows_router.add_api_route(
            path="/metadata",
            endpoint=self.fetch_metadata,
            methods=["POST"],
            response_model=dict,
        )

        self.workflows_router.add_api_route(
            path="/check",
            endpoint=self.preflight_check,
            methods=["POST"],
            response_model=dict,
        )

        self.workflows_router.add_api_route(
            path="/start",
            endpoint=self.start_workflow,
            methods=["POST"],
            response_model=dict,
        )

        self.app.include_router(self.workflows_router)
