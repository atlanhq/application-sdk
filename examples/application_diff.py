import asyncio
from typing import Any, Dict

from application_sdk.clients.temporal import TemporalClient
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.workers.atlan.utilities import UtilitiesAtlanWorker
from application_sdk.workflows.atlan.utilities import UtilitiesAtlanWorkflow

APPLICATION_NAME = "diff"

logger = get_logger(__name__)


async def application_diff() -> Dict[str, Any]:
    utilities_client = TemporalClient(application_name=UtilitiesAtlanWorker.TASK_QUEUE)
    await utilities_client.load()

    utilities_worker = UtilitiesAtlanWorker(client=utilities_client)

    workflow_response = await utilities_client.start_workflow(
        workflow_args={}, workflow_class=UtilitiesAtlanWorkflow
    )

    await utilities_worker.start()
    return workflow_response


if __name__ == "__main__":
    asyncio.run(application_diff())
