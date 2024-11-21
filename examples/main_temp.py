from typing import Annotated, Any, Callable, Dict

import uvicorn
from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class TestAuthRequest(BaseModel):
    name: str
    description: str | None = Field(
        default=None, title="The description of the item", max_length=300
    )


def validation_middleware(request_body_dto):
    def validation(fn):
        async def decorator(self, request: Request):
            try:
                request_body_dto.model_validate(await request.json())
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

        self.app.include_router(self.workflow_router, prefix="/workflows/v1")
        self.app.include_router(self.preflight_router, prefix="/preflight/v1")
        self.app.include_router(self.auth_router, prefix="/auth/v1")

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

    @http_controller
    @validation_middleware(TestAuthRequest)
    def test_auth(self, testAuthRequest: TestAuthRequest):
        return {"success": True, "abcd": self.abcd}

    def preflight_test(self):
        return {"success": True, "abcd": self.abcd}

    def start(self):
        uvicorn.run(self.app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    fast_api_app = FastAPIApplication(app=FastAPI())
    fast_api_app.start()
