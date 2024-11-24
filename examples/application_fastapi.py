import asyncio
from typing import Any, Dict, List

from application_sdk.app.rest.fastapi import FastAPIApplication
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.workflow import WorkflowInterface


class WorkflowAuthController(WorkflowAuthControllerInterface):
    async def test_auth(self, credential: Dict[str, Any]) -> bool:
        return True


class WorkflowMetadataController(WorkflowMetadataControllerInterface):
    async def fetch_metadata(self, credential: Dict[str, str]) -> List[Dict[str, str]]:
        return [{"database": "test", "schema": "test"}]


class WorkflowPreflightCheckController(WorkflowPreflightCheckControllerInterface):
    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"databaseSchemaCheck": ["test"], "tablesCheck": ["test"]}


class SampleWorkflow(WorkflowInterface):
    async def start(self, workflow_args: Dict[str, Any], workflow_class: Any) -> None:
        pass

    async def run(self, workflow_args: Dict[str, Any]) -> None:
        pass


if __name__ == "__main__":
    fast_api_app = FastAPIApplication(
        auth_controller=WorkflowAuthController(),
        metadata_controller=WorkflowMetadataController(),
        preflight_check_controller=WorkflowPreflightCheckController(),
        workflow=SampleWorkflow(),
    )
    asyncio.run(fast_api_app.start())
