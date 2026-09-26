"""Temporal-backed :class:`WorkflowStarter` — the ``[workflow]`` extra only.

This module hard-imports ``temporalio`` and MUST NOT be imported by the core.
It is loaded lazily (see :func:`server_sdk.workflow.starter_from_env`) after
confirming :data:`server_sdk.workflow.WORKFLOW_EXTRA_AVAILABLE`. In a
consolidated serving image the extra is not installed and this file is never
imported.

Naming follows the platform convention so a started workflow lands on the
app's real worker:

    workflow name:  ``<app_name>``                (default entry point)
                    ``<app_name>:<entrypoint>``   (explicit entry point)
    task queue:     ``atlan-<app_name>-<deployment>``  (unless overridden)
    workflow id:    caller-supplied, else ``<app_name>-<8 hex chars>``

The request body is passed as the workflow's single argument; the worker's
input model validates it on receipt. (The worker-side config-hash workflow-id
scheme needs the app's input models, so ids here use a plain random suffix.)
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from server_sdk.manifest import worker_task_queue
from server_sdk.observability.logger_adaptor import get_logger
from server_sdk.workflow import StartRequest, StartResult
from temporalio.client import Client

logger = get_logger(__name__)


class TemporalWorkflowStarter:
    """Dispatches ``/workflows/v1/start`` requests to a Temporal frontend."""

    def __init__(
        self,
        *,
        app_name: str,
        host: str,
        namespace: str = "default",
        task_queue: str | None = None,
        execution_timeout_hours: int | None = None,
    ) -> None:
        self._app_name = app_name
        self._host = host
        self._namespace = namespace
        # The queue the app's WORKER actually polls. Platform convention is
        # atlan-<app>-<deployment> — the same string the generated manifests carry
        # and AE dispatches to. A guess like f"{app_name}-queue" is accepted by
        # Temporal and then nobody polls it: /start answers 200 with a real
        # workflow id and the work never runs. Derived from the app name passed in,
        # never from ATLAN_APPLICATION_NAME (which names the HOST when apps share
        # a process).
        self._task_queue = task_queue or worker_task_queue(app_name)
        self._execution_timeout_hours = execution_timeout_hours
        self._client: Client | None = None

    async def _get_client(self) -> Client:
        if self._client is None:
            logger.info(
                "Connecting to Temporal host=%s namespace=%s",
                self._host,
                self._namespace,
            )
            self._client = await Client.connect(self._host, namespace=self._namespace)
        return self._client

    async def start(self, request: StartRequest) -> StartResult:
        client = await self._get_client()
        workflow_name = (
            f"{self._app_name}:{request.entrypoint}"
            if request.entrypoint
            else self._app_name
        )
        workflow_id = request.workflow_id or f"{self._app_name}-{uuid4().hex[:8]}"
        # Inject the framework-managed fields the worker's Input model expects
        # (workflow_id / correlation_id are populated at dispatch time).
        arg = {
            **request.body,
            "workflow_id": workflow_id,
            "correlation_id": request.correlation_id,
        }
        handle = await client.start_workflow(
            workflow_name,
            args=[arg],
            id=workflow_id,
            task_queue=self._task_queue,
            memo={"correlation_id": request.correlation_id},
            execution_timeout=(
                timedelta(hours=self._execution_timeout_hours)
                if self._execution_timeout_hours
                else None
            ),
        )
        logger.info(
            "Workflow started: app=%s workflow=%s workflow_id=%s run_id=%s queue=%s",
            request.app_name,
            workflow_name,
            handle.id,
            handle.result_run_id,
            self._task_queue,
        )
        return StartResult(workflow_id=handle.id, run_id=handle.result_run_id)


__all__ = ["TemporalWorkflowStarter"]
