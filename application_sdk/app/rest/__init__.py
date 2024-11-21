from functools import wraps
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


class AtlanAPIApplication:
    pass

class FastAPIApplication(AtlanAPIApplication):
    app: FastAPI


    def __init__(self):




def my_decorator(route, router, *args, **kwargs):
    """
    A custom decorator to register a FastAPI route dynamically.

    :param route: The path for the route (e.g., "/testing").
    :param router: The APIRouter instance where the route should be registered.
    :param args: Additional arguments to pass to add_api_route.
    :param kwargs: Additional keyword arguments to pass to add_api_route.
    """
    def decorator(func):
        @wraps(func)  # Preserve the function's metadata
        async def wrapper(self, *func_args, **func_kwargs):
            return await func(self, *func_args, **func_kwargs)

        # Register the route with the router
        router.add_api_route(route, wrapper, *args, **kwargs)
        return func

    return decorator

class FastAPIApplicationBuilder(AtlanApplicationBuilder):
    workflows_router: APIRouter = APIRouter(
        prefix="/workflows/v1",
        tags=["workflows"]
    )
    logs_router: APIRouter = logs.router
    metrics_router: APIRouter = metrics.router
    traces_router: APIRouter = traces.router

    testingString = "testing"

    app: FastAPI

    @my_decorator(router = workflows_router, route = "/testing")
    async def testing(self):
        return {"success": True, "message": self.testingString}

    auth_controller: WorkflowAuthControllerInterface | None
    metadata_controller: WorkflowMetadataControllerInterface | None
    preflight_check_controller: WorkflowPreflightCheckControllerInterface | None

    resource: ResourceInterface | None
    workflow: WorkflowInterface | None

    def __init__(
        self,
        app: FastAPI,
        workflow: WorkflowInterface | None = None,
        resource: Optional[ResourceInterface] | None = None,
        auth_controller: Optional[WorkflowAuthControllerInterface] | None = None,
        metadata_controller: Optional[WorkflowMetadataControllerInterface]
        | None = None,
        preflight_check_controller: Optional[WorkflowPreflightCheckControllerInterface]
        | None = None,
    ):
        self.app = app
        self.app.include_router(self.workflows_router)

    def add_telemetry_routes(self) -> None:
        pass
        # self.app.include_router(logs.router)
        # self.app.include_router(metrics.router)
        # self.app.include_router(traces.router)

    def on_api_service_start(self):
        pass
        # super().on_api_service_start()
        # FastAPIInstrumentor.instrument_app(self.app)  # pyright: ignore[reportUnknownMemberType]

    async def test_auth(self, credential: Dict[str, Any]):
        pass
        # if not self.auth_controller:
        #     raise HTTPException(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         detail="Auth interface not implemented",
        #     )
        # try:
        #     self.auth_controller.test_auth()
        #     return JSONResponse(
        #         status_code=status.HTTP_200_OK,
        #         content={
        #             "success": True,
        #             "message": "Authentication successful",
        #         },
        #     )
        # except Exception as e:
        #     return JSONResponse(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         content={
        #             "success": False,
        #             "message": "Failed to test authentication",
        #             "error": str(e),
        #         },
        #     )

    async def fetch_metadata(self, credential: Dict[str, Any]):
        pass
        # if not self.metadata_controller:
        #     raise HTTPException(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         detail="Metadata interface not implemented",
        #     )

        # if not self.resource:
        #     raise HTTPException(status_code=500, detail="Resource not implemented")
        # try:
        #     self.resource.set_credentials(credential)
        #     await self.resource.load()

        #     return JSONResponse(
        #         status_code=status.HTTP_200_OK,
        #         content={
        #             "success": True,
        #             "data": await self.metadata_controller.fetch_metadata(credential),
        #         },
        #     )
        # except Exception as e:
        #     return JSONResponse(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         content={
        #             "success": False,
        #             "message": "Failed to fetch metadata",
        #             "error": str(e),
        #         },
        #     )

    async def preflight_check(self, form_data: Dict[str, Any]):
        pass
        # if not self.preflight_check_controller:
        #     raise HTTPException(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         detail="Preflight check controller not implemented",
        #     )

        # if not self.resource:
        #     raise HTTPException(status_code=500, detail="Resource not implemented")
        # try:
        #     if not form_data["credentials"]:
        #         raise HTTPException(
        #             status_code=status.HTTP_400_BAD_REQUEST,
        #             detail="Credentials not passed",
        #         )

        #     return JSONResponse(
        #         status_code=status.HTTP_200_OK,
        #         content={
        #             "success": True,
        #             "data": await self.preflight_check_controller.preflight_check(
        #                 form_data
        #             ),
        #         },
        #     )
        # except Exception as e:
        #     return JSONResponse(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         content={
        #             "success": False,
        #             "message": "Preflight check failed",
        #             "error": str(e),
        #         },
        #     )

    async def start_workflow(self, workflow_args: Dict[str, Any]):
        pass
        # if not self.workflow:
        #     raise HTTPException(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        #         detail="Workflow interface not implemented",
        #     )
        # try:
        #     workflow_metadata = await self.workflow.start(
        #         workflow_args, workflow_class=self.workflow.__class__
        #     )
        #     return JSONResponse(
        #         status_code=status.HTTP_200_OK,
        #         content={
        #             "success": True,
        #             "message": "Workflow started successfully",
        #             "data": workflow_metadata,
        #         },
        #     )
        # except Exception as e:
        #     raise HTTPException(
        #         status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        #     )

    def add_workflows_router(self):
        pass
        # self.workflows_router.add_api_route(
        #     path="/auth",
        #     endpoint=self.test_auth,
        #     methods=["POST"],
        #     response_model=bool,
        # )

        # self.workflows_router.add_api_route(
        #     path="/metadata",
        #     endpoint=self.fetch_metadata,
        #     methods=["POST"],
        #     response_model=dict,
        # )

        # self.workflows_router.add_api_route(
        #     path="/check",
        #     endpoint=self.preflight_check,
        #     methods=["POST"],
        #     response_model=dict,
        # )

        # self.workflows_router.add_api_route(
        #     path="/start",
        #     endpoint=self.start_workflow,
        #     methods=["POST"],
        #     response_model=dict,
        # )

        # self.app.include_router(self.workflows_router)

    # @workflows_router.post("/auth")
    # @validation()
    # @error_handler()
    # async def test_auth(self, credential: Dict[str, Any]):
    #     pass

    # @workflows_router.post("/start")
    # @validation()
    # @error_handler()
    # async def start_workflow(self):
    #     pass
