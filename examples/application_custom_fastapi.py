import uuid
from typing import Any, Dict, List

from fastapi import APIRouter

from application_sdk.app.rest.fastapi import FastAPIApplication
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.workflow import WorkflowInterface


class WorkflowAuthController(WorkflowAuthControllerInterface):
    async def prepare(self, credentials: Dict[str, Any]) -> None:
        pass

    async def test_auth(self) -> bool:
        return True


class WorkflowMetadataController(WorkflowMetadataControllerInterface):
    async def prepare(self, credentials: Dict[str, Any]) -> None:
        pass

    async def fetch_metadata(self) -> List[Dict[str, str]]:
        return [{"database": "test", "schema": "test"}]


class WorkflowPreflightCheckController(WorkflowPreflightCheckControllerInterface):
    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"databaseSchemaCheck": ["test"], "tablesCheck": ["test"]}


class SampleWorkflow(WorkflowInterface):
    async def start(self, workflow_args: Dict[str, Any], workflow_class: Any) -> None:
        return {
            "workflow_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
        }

    async def run(self, workflow_args: Dict[str, Any]) -> None:
        pass


class MyCustomFastAPIApplication(FastAPIApplication):
    custom_router: APIRouter = APIRouter()

    def register_routers(self):
        self.app.include_router(self.custom_router, prefix="/custom")
        super().register_routers()

    def register_routes(self):
        self.custom_router.add_api_route(
            "/test",
            self.test,
            methods=["GET"],
        )

        super().register_routes()

    async def test(self, **kwargs) -> Dict[str, str]:
        return {"message": "Hello, World!"}


if __name__ == "__main__":
    fast_api_app = MyCustomFastAPIApplication(
        auth_controller=WorkflowAuthController(),
        metadata_controller=WorkflowMetadataController(),
        preflight_check_controller=WorkflowPreflightCheckController(),
        workflow=SampleWorkflow(),
    )

    # Uncomment to run the application locally
    # asyncio.run(fast_api_app.start())
