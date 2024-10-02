from application_sdk.app import AtlanApplicationBuilder, WorkflowBuilderInterface
from application_sdk.app.rest.fastapi.routers import health, logs, metrics, traces

from fastapi import APIRouter, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
import asyncio
from typing import Optional

class FastAPIApplicationBuilder(AtlanApplicationBuilder):
    workflows_router: APIRouter = APIRouter(
        prefix="/workflows/v1",
        tags=["workflows"],
        responses={404: {"description": "Not found"}},
    )

    def __init__(
        self,
        app: FastAPI,
        workflow_builder_interface: Optional[WorkflowBuilderInterface] = None,
    ):
        self.app = app
        self.app.include_router(health.router)
        super().__init__(workflow_builder_interface)

    def add_telemetry_routes(self) -> None:
        self.app.include_router(logs.router)
        self.app.include_router(metrics.router)
        self.app.include_router(traces.router)


    def on_api_service_start(self):
        super().on_api_service_start()
        # FastAPIInstrumentor.instrument_app(self.app)  # pyright: ignore[reportUnknownMemberType]

    def start_worker(self):
        if (
            self.workflow_builder_interface
            and self.workflow_builder_interface.worker_interface
        ):
            # Start worker in a separate thread
            asyncio.run(self.workflow_builder_interface.worker_interface.start_worker())

    async def test_auth(self, credential: dict):
        if (
            not self.workflow_builder_interface
            or not self.workflow_builder_interface.auth_interface
        ):
            raise HTTPException(
                status_code=500, detail="Auth interface not implemented"
            )
        try:
            self.workflow_builder_interface.auth_interface.test_auth(credential)
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "Authentication successful",
                },
            )
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "message": "Failed to test authentication",
                    "error": str(e),
                },
            )

    async def fetch_metadata(self, credential: dict):
        if (
            not self.workflow_builder_interface
            or not self.workflow_builder_interface.metadata_interface
        ):
            raise HTTPException(
                status_code=500, detail="Metadata interface not implemented"
            )
        try:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "data": self.workflow_builder_interface.metadata_interface.fetch_metadata(
                        credential
                    ),
                },
            )
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "message": "Failed to fetch metadata",
                    "error": str(e),
                },
            )

    async def preflight_check(self, form_data: dict):
        if (
            not self.workflow_builder_interface
            or not self.workflow_builder_interface.preflight_check_interface
        ):
            raise HTTPException(
                status_code=500, detail="Preflight check interface not implemented"
            )
        try:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "data": self.workflow_builder_interface.preflight_check_interface.preflight_check(
                        form_data
                    ),
                },
            )
        except Exception as e:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "success": False,
                    "message": "Preflight check failed",
                    "error": str(e),
                },
            )

    async def start_workflow(self, workflow_args: dict):
        if (
            not self.workflow_builder_interface
            or not self.workflow_builder_interface.worker_interface
        ):
            raise HTTPException(
                status_code=500, detail="Worker interface not implemented"
            )
        try:
            await self.workflow_builder_interface.worker_interface.start_workflow(
                workflow_args
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "Workflow started successfully",
                },
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

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
