import asyncio
import uuid
from typing import Any, Dict

from fastapi import APIRouter

from application_sdk.handlers import HandlerInterface
from application_sdk.server.fastapi import APIServer, HttpWorkflowTrigger
from application_sdk.workflows import WorkflowInterface


class CustomHandler(HandlerInterface):
    async def load(self, **kwargs: Any) -> None:
        pass

    async def test_auth(self, **kwargs: Any) -> bool:
        return True

    async def fetch_metadata(self, **kwargs: Any) -> Any:
        return [{"database": "test", "schema": "test"}]

    async def preflight_check(self, **kwargs: Any) -> Any:
        return {"databaseSchemaCheck": ["test"], "tablesCheck": ["test"]}


class SampleWorkflow(WorkflowInterface):
    async def start(
        self, workflow_args: Dict[str, Any], workflow_class: Any
    ) -> Dict[str, str]:
        return {
            "workflow_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
        }

    async def run(self, workflow_config: Dict[str, Any]) -> None:
        pass


class MyCustomApplication(APIServer):
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

    async def test(self, **kwargs: Dict[str, Any]) -> Dict[str, str]:
        return {"message": "Hello, World!"}


async def application_custom_fastapi():
    fast_api_app = MyCustomApplication(handler=CustomHandler())
    fast_api_app.register_workflow(
        SampleWorkflow,
        [
            HttpWorkflowTrigger(
                endpoint="/sample",
                methods=["POST"],
                workflow_class=SampleWorkflow,
            )
        ],
    )

    await fast_api_app.start()


if __name__ == "__main__":
    asyncio.run(application_custom_fastapi())
