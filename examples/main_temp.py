from typing import Annotated, Any, Callable, Dict

import uvicorn
from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class TestAuthRequest(BaseModel):
    name: str = Field(description="The name of the item")
    description: str | None = Field(
        default=None, title="The description of the item", max_length=300
    )


class TestAuthResponse(BaseModel):
    success: bool
    abcd: str


def validation_middleware(request_body_dto):
    def validation(fn):
        async def decorator(self, request: Request):
            try:
                print(self.abcd)
                json_data = await request.json()
                request_body_dto.model_validate(json_data)
                return await fn(
                    self,
                    body=request_body_dto.parse_obj(json_data),
                    query_params=request.query_params,
                    headers=request.headers,
                    request=request,
                )
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

        return decorator

    return validation


def http_controller(fn):
    async def decorator(self, request: Request):
        try:
            result = await fn(self, request)
            return JSONResponse(status_code=200, content=result)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"error": e.detail})
        except Exception as e:
            # Log
            return JSONResponse(status_code=500, content={"error": str(e)})

    return decorator


class AtlanAPIApplication:
    pass


class FastAPIApplication(AtlanAPIApplication):
    app: FastAPI = FastAPI()

    workflow_router: APIRouter
    preflight_router: APIRouter
    auth_router: APIRouter

    controllerA = None
    controllerB = None
    abcd = "abcd"

    def __init__(
        self,
        app: FastAPI,
        controllerA: None = None,
        controllerB: None = None,
    ):
        self.app = app

        self.controllerA = controllerA
        self.controllerB = controllerB

        self.workflow_router = APIRouter()
        self.preflight_router = APIRouter()
        self.auth_router = APIRouter()

        self.register_routes()
        self.register_routers()

    def register_routes(self):
        self.workflow_router.add_api_route(
            "/test_auth", endpoint=self.test_auth, methods=["POST"], response_model=dict
        )
        # ...

        self.preflight_router.add_api_route(
            "/preflight", self.preflight_test, methods=["GET"]
        )
        # ...

        self.auth_router.add_api_route("/test_auth", self.test_auth, methods=["GET"])
        # ...

    def register_routers(self):
        self.app.include_router(self.workflow_router, prefix="/workflows/v1")
        self.app.include_router(self.preflight_router, prefix="/preflight/v1")
        self.app.include_router(self.auth_router, prefix="/auth/v1")

    @http_controller
    @validation_middleware(TestAuthRequest)
    def test_auth(
        self,
        body: TestAuthRequest,
        query_params: Dict[str, Any],
        headers: Dict[str, Any],
        request: Request,
    ) -> TestAuthResponse:
        return TestAuthResponse(success=True, abcd=self.abcd)

    def preflight_test(self):
        return {"success": True, "abcd": self.abcd}

    def start(self):
        uvicorn.run(self.app, host="0.0.0.0", port=8000)


class MyFastAPIApplication(FastAPIApplication):
    my_custom_router = APIRouter()

    def register_routes(self):
        self.my_custom_router.add_api_route(
            "/my_custom_route", self.my_custom_route, methods=["GET"]
        )

        super().register_routes()

    def register_routers(self):
        self.app.include_router(self.my_custom_router, prefix="/my_custom_router")

        super().register_routers()

    @http_controller
    @validation_middleware(TestAuthRequest)
    def my_custom_route(self, testAuthRequest: TestAuthRequest):
        return {"success": True, "abcd": self.abcd}


if __name__ == "__main__":
    fast_api_app = FastAPIApplication(app=FastAPI())
    fast_api_app.start()
