"""
Utility functions for monitoring Temporal workflow execution status.
"""

import asyncio
import logging
import time
from typing import Any, Optional

from temporalio.client import WorkflowExecutionStatus, WorkflowHandle

from application_sdk.workflows.resources.temporal_resource import (
    TemporalConfig,
    TemporalResource,
)
from examples.application_sql import application_sql
from examples.application_sql_miner import application_sql_miner
from examples.application_sql_with_custom_transformer import (
    application_sql_with_custom_transformer,
)
from examples.application_subscriber import application_subscriber

logger = logging.getLogger(__name__)


async def monitor_workflow_execution_and_write_status(
    workflow_handle: WorkflowHandle[Any, Any],
    polling_interval: int = 10,
    timeout: Optional[int] = None,
) -> str:
    """
    Monitor a Temporal workflow execution until completion or timeout.

    Args:
        workflow_handle: The Temporal workflow handle to monitor
        polling_interval: Time in seconds between status checks (default: 10)
        timeout: Maximum time in seconds to monitor (default: None, meaning no timeout)

    Returns:
        str: Final status of the workflow

    Raises:
        TimeoutError: If the workflow monitoring exceeds the specified timeout
        Exception: If there's an error while monitoring the workflow
    """
    start_time = time.time()

    status = "RUNNING 🟡"

    try:
        while True:
            workflow_execution = await workflow_handle.describe()
            current_status = (
                workflow_execution.raw_description.workflow_execution_info.status
            )

            if current_status != WorkflowExecutionStatus.RUNNING:
                if current_status == WorkflowExecutionStatus.COMPLETED:
                    logger.info(
                        f"Workflow completed with status: {WorkflowExecutionStatus.COMPLETED.name}"
                    )
                    status = "COMPLETED 🟢"
                    break
                else:
                    logger.info(
                        f"Workflow failed with status: {WorkflowExecutionStatus.FAILED.name}"
                    )
                    status = "FAILED 🔴"
                    break

            if timeout and (time.time() - start_time) > timeout:
                logger.info(f"Workflow monitoring timed out after {timeout} seconds")
                status = "FAILED 🔴"
                break

            logger.debug(
                f"Workflow is still running. Checking again in {polling_interval} seconds"
            )
            await asyncio.sleep(polling_interval)

    except Exception as e:
        logger.error(f"Error monitoring workflow: {str(e)}")
        status = "FAILED 🔴"

    return status


async def main():
    temporal_resource = TemporalResource(TemporalConfig())
    await temporal_resource.load()
    # run all the examples

    with open("workflow_status.md", "w") as f:
        f.write("## 📦 Example workflows test results\n")
        f.write("- This workflow runs all the examples in the `examples` directory.\n")
        f.write("-----------------------------------\n")
        f.write("| Example | Status | Time Taken |\n")
        f.write("| --- | --- | --- |\n")

    examples = [
        application_sql,
        application_sql_with_custom_transformer,
        application_sql_miner,
        application_subscriber,
    ]

    for example in examples:
        start_time = time.time()
        workflow_response = await example()

        workflow_handle = temporal_resource.client.get_workflow_handle(
            workflow_id=workflow_response["workflow_id"],
            run_id=workflow_response["run_id"],
        )

        status = await monitor_workflow_execution_and_write_status(
            workflow_handle,
            polling_interval=5,
            timeout=120,
        )
        end_time = time.time()
        time_taken = end_time - start_time
        time_taken_formatted = f"{time_taken:.2f} seconds"

        with open("workflow_status.md", "a") as f:
            f.write(f"| {example.__name__} | {status} | {time_taken_formatted} |\n")

    with open("workflow_status.md", "a") as f:
        f.write(
            "> This is an automatically generated file. Please do not edit directly.\n"
        )


if __name__ == "__main__":
    asyncio.run(main())
