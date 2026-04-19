"""Workflow trigger and status helpers for K8s e2e tests."""

import asyncio
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.e2e.portforward import kube_http_call

logger = get_logger(__name__)

_TERMINAL_STATES = {"completed", "failed", "cancelled", "terminated"}


async def run_workflow(
    namespace: str,
    service: str,
    port: int,
    workflow_name: str,
    payload: dict[str, Any],
) -> str:
    """POST to the handler's workflow endpoint and return the workflow ID.

    Args:
        namespace: K8s namespace where the handler service lives.
        service: Handler service name.
        port: Handler service port.
        workflow_name: Name of the workflow to trigger.
        payload: JSON body for the workflow request.

    Returns:
        The workflow ID string from the response.
    """
    response = await kube_http_call(
        namespace=namespace,
        service=service,
        port=port,
        method="POST",
        path="/api/v1/workflows",
        body={"workflow_name": workflow_name, **payload},
    )
    response.raise_for_status()
    data = response.json()
    workflow_id: str = data["workflow_id"]
    logger.info("Started workflow %s (id=%s)", workflow_name, workflow_id)
    return workflow_id


async def wait_for_workflow(
    namespace: str,
    service: str,
    port: int,
    workflow_id: str,
    timeout: float = 300.0,
    poll_interval: float = 5.0,
) -> dict[str, Any]:
    """Poll GET /api/v1/workflows/{id} until the workflow reaches a terminal state.

    Args:
        namespace: K8s namespace where the handler service lives.
        service: Handler service name.
        port: Handler service port.
        workflow_id: Workflow ID returned by :func:`run_workflow`.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.

    Returns:
        The final workflow status dict from the last response.

    Raises:
        TimeoutError: If the workflow does not complete within ``timeout``.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await kube_http_call(
            namespace=namespace,
            service=service,
            port=port,
            method="GET",
            path=f"/api/v1/workflows/{workflow_id}",
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        status = data.get("status", "").lower()
        logger.debug("Workflow %s status: %s", workflow_id, status)
        if status in _TERMINAL_STATES:
            return data
        await asyncio.sleep(poll_interval)

    raise TimeoutError(
        f"Workflow {workflow_id} did not reach a terminal state within {timeout}s"
    )
