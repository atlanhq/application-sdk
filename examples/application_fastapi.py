import uuid
from typing import Any, Dict, List

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
        return {
            "success": True,
            "data": {
                "databaseSchemaCheck": {
                    "success": True,
                    "successMessage": "Schemas and Databases check successful",
                    "failureMessage": "",
                },
                "tablesCheck": {
                    "success": True,
                    "successMessage": "Tables check successful. Table count: 2",
                    "failureMessage": "",
                },
            },
        }


class SampleWorkflow(WorkflowInterface):
    async def start(self, workflow_args: Dict[str, Any], workflow_class: Any) -> None:
        return {
            "workflow_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
        }

    async def run(self, workflow_config: Dict[str, Any]) -> None:
        pass


if __name__ == "__main__":
    fast_api_app = FastAPIApplication(
        auth_controller=WorkflowAuthController(),
        metadata_controller=WorkflowMetadataController(),
        preflight_check_controller=WorkflowPreflightCheckController(),
        workflow=SampleWorkflow(),
    )

    # Uncomment this to start the app locally
    # import asyncio
    # asyncio.run(fast_api_app.start())
