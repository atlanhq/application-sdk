import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from application_sdk.handlers.sql import BaseSQLHandler
from application_sdk.server.fastapi import APIServer, HttpWorkflowTrigger
from application_sdk.workflows import WorkflowInterface


class SampleSQLHandler(BaseSQLHandler):
    async def prepare(self, credentials: Dict[str, Any], **kwargs) -> None:
        pass

    async def test_auth(self, **kwargs: Any) -> bool:
        return True

    async def fetch_metadata(
        self,
        metadata_type: Optional[str] = None,
        database: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, str]]:
        return [{"database": "test", "schema": "test"}]

    async def preflight_check(
        self, payload: Dict[str, Any], **kwargs: Any
    ) -> Dict[str, Any]:
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
    async def run(self, workflow_config: Dict[str, Any]) -> None:
        pass

    async def get_configmap(self, config_map_id: str) -> Dict[str, Any]:
        workflow_json_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "application_sdk",
            "frontend",
            "content",
            "workflow.json",
        )

        credential_json_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "application_sdk",
            "frontend",
            "content",
            "credential.json",
        )

        if config_map_id == "atlan-connectors-redshift":
            with open(credential_json_path) as f:
                return json.load(f)

        with open(workflow_json_path) as f:
            return json.load(f)


async def application_fastapi():
    fast_api_app = APIServer(
        handler=SampleSQLHandler(),
    )
    fast_api_app.register_workflow(
        SampleWorkflow,
        [
            HttpWorkflowTrigger(
                workflow_class=SampleWorkflow,
            ),
            HttpWorkflowTrigger(
                endpoint="/sample",
                methods=["POST"],
                workflow_class=SampleWorkflow,
            ),
        ],
    )

    await fast_api_app.start()


if __name__ == "__main__":
    asyncio.run(application_fastapi())
