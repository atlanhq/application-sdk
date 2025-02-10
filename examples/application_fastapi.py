import asyncio
from typing import Any, Dict, List

import uvloop

from application_sdk.application.fastapi import FastAPIApplication, HttpWorkflowTrigger
from application_sdk.handlers.sql import SQLHandler
from application_sdk.workflows import WorkflowInterface

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())


class SampleSQLHandler(SQLHandler):
    async def prepare(self, credentials: Dict[str, Any], **kwargs) -> None:
        pass

    async def test_auth(self, **kwargs) -> bool:
        return True

    async def fetch_metadata(self, **kwargs) -> List[Dict[str, str]]:
        return [{"database": "test", "schema": "test"}]

    async def preflight_check(
        self, payload: Dict[str, Any], **kwargs
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


async def application_fastapi():
    fast_api_app = FastAPIApplication(
        handler=SampleSQLHandler(),
    )
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
    asyncio.run(application_fastapi())
