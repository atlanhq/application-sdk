"""
Utility functions for monitoring Temporal workflow execution status.
"""

import asyncio
import logging
import time
from typing import Any, Optional

from temporalio.client import WorkflowExecutionStatus, WorkflowHandle

from application_sdk.clients.workflow import WorkflowClient

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


async def run_and_monitor_workflow(
    example_workflow_function,
    workflow_client: WorkflowClient,
    polling_interval: int = 5,
    timeout: Optional[int] = 240,
) -> tuple[str, float]:
    """
    Run and monitor a workflow example, returning its status and execution time.

    Args:
        example: The workflow example function to run
        workflow_client: The temporal client instance

    Returns:
        tuple[str, float]: A tuple containing (workflow_status, time_taken)
    """
    start_time = time.time()
    workflow_response = await example_workflow_function()

    workflow_handle = workflow_client.client.get_workflow_handle(
        workflow_id=workflow_response["workflow_id"],
        run_id=workflow_response["run_id"],
    )

    status = await monitor_workflow_execution_and_write_status(
        workflow_handle,
        polling_interval=polling_interval,
        timeout=timeout,
    )
    end_time = time.time()
    time_taken = end_time - start_time

    return status, time_taken
