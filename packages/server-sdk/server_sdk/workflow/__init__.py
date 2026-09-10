"""``/workflows/v1/start`` surface — gated behind the ``[workflow]`` extra.

The core imports NOTHING from this package at import time other than the
pure-Python seam here. ``temporalio`` lives only in
:mod:`server_sdk.workflow.temporal`, imported lazily and only when the
``[workflow]`` extra is installed. In the consolidated serving image the extra
is absent, so :data:`WORKFLOW_EXTRA_AVAILABLE` is ``False``, the route is never
registered, and ``/workflows/v1/start`` 404s. An app running standalone installs
``atlan-server-sdk[workflow]`` and gets a functional ``/start`` that dispatches
to its own worker.

The route parses the request and emits the standard success/error envelopes; the
Temporal-specific dispatch is delegated to an injectable :class:`WorkflowStarter`.
"""

from __future__ import annotations

import importlib.util
import os
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import uuid4

if TYPE_CHECKING:
    from server_sdk.workflow.temporal import TemporalWorkflowStarter

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from server_sdk.handler.contracts import normalize_credentials
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# True iff the [workflow] extra (temporalio) is importable. Only presence is
# probed — no temporalio symbol is imported at module scope.
WORKFLOW_EXTRA_AVAILABLE: bool = importlib.util.find_spec("temporalio") is not None


@dataclass
class StartRequest:
    """The parsed, credential-stripped /start request handed to a starter."""

    app_name: str
    entrypoint: str | None
    workflow_id: str | None
    correlation_id: str
    body: dict[str, Any] = field(default_factory=dict)


@dataclass
class StartResult:
    """What a starter returns after dispatching a workflow."""

    workflow_id: str
    run_id: str


@runtime_checkable
class WorkflowStarter(Protocol):
    """Dispatches a workflow. The Temporal impl lives behind the ``[workflow]`` extra."""

    async def start(self, request: StartRequest) -> StartResult: ...


def starter_from_env(app_name: str) -> "TemporalWorkflowStarter | None":
    """Build the default Temporal starter from the environment, or ``None``.

    Returns a starter only when the ``[workflow]`` extra is installed AND
    ``ATLAN_TEMPORAL_HOST`` is set (unset → ``/start`` answers 503 "not
    configured"). Optional overrides: ``ATLAN_TEMPORAL_NAMESPACE`` (default
    ``default``). ``ATLAN_TASK_QUEUE`` is deliberately NOT read: it is a single
    process-global value and a consolidated host serves many apps, so it can only
    ever be correct for one of them. The queue is derived per app instead
    (``atlan-<app_name>-<deployment>``); pass ``task_queue`` explicitly to override.
    """
    if not WORKFLOW_EXTRA_AVAILABLE:
        return None
    host = os.getenv("ATLAN_TEMPORAL_HOST", "")
    if not host:
        return None
    from server_sdk.workflow.temporal import (  # noqa: PLC0415 — imports temporalio; gated above
        TemporalWorkflowStarter,
    )

    return TemporalWorkflowStarter(
        app_name=app_name,
        host=host,
        namespace=os.getenv("ATLAN_TEMPORAL_NAMESPACE", "default"),
        task_queue=None,
    )


def register_start_route(
    app: FastAPI,
    *,
    app_name: str,
    starter: WorkflowStarter | None,
    default_entrypoint: str | None = None,
) -> None:
    """Register ``POST /workflows/v1/start``.

    Registered when a starter is injected or the ``[workflow]`` extra is present.
    With no ``starter`` the route returns 503 "not configured". A request must
    select an entrypoint — ``?entrypoint=<name>`` or the configured
    ``default_entrypoint`` — otherwise it returns 400 rather than dispatching a
    bare ``<app_name>`` workflow type the worker may not have registered (a
    started-but-never-runnable workflow that would report success and then hang).
    """

    @app.post("/workflows/v1/start")
    async def start_workflow(request: Request) -> JSONResponse:
        if starter is None:
            raise HTTPException(
                status_code=503,
                detail="Workflow execution not configured. Set ATLAN_TEMPORAL_HOST.",
            )

        body: dict[str, Any] = await request.json()
        explicit_workflow_id: str | None = body.pop("workflow_id", None)
        entrypoint_param: str | None = request.query_params.get("entrypoint")
        legacy_workflow_type: str | None = body.pop("workflow_type", None)
        selected_entrypoint: str | None = (
            entrypoint_param or legacy_workflow_type or default_entrypoint
        )
        workflow_id = explicit_workflow_id or "(unknown)"

        if selected_entrypoint is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "entrypoint is required: pass ?entrypoint=<name> "
                    "(or configure a default_entrypoint on the server)."
                ),
            )

        if legacy_workflow_type is not None and entrypoint_param is None:
            warnings.warn(
                f"App {app_name}: 'workflow_type' body field is deprecated. "
                "Use ?entrypoint=<name> query param instead. "
                "Will be removed in v4.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            logger.warning(
                "App %s: 'workflow_type' body field is deprecated; "
                "use ?entrypoint=<name> query param instead.",
                app_name,
            )

        try:
            body = normalize_credentials(body)
            # Never dispatch credential material into workflow history. After
            # normalize_credentials, all of it lives under "credentials".
            body.pop("credentials", None)

            correlation_id = body.get("correlation_id") or str(uuid4())
            result = await starter.start(
                StartRequest(
                    app_name=app_name,
                    entrypoint=selected_entrypoint,
                    workflow_id=explicit_workflow_id,
                    correlation_id=correlation_id,
                    body=body,
                )
            )
            return JSONResponse(
                content={
                    "success": True,
                    "message": "Workflow started successfully",
                    "data": {
                        "workflow_id": result.workflow_id,
                        "run_id": result.run_id,
                    },
                    "correlation_id": correlation_id,
                }
            )
        except HTTPException:
            raise
        except TypeError as e:
            logger.error(
                "Invalid workflow input for app %s: %s", app_name, e, exc_info=True
            )
            raise HTTPException(status_code=400, detail="Invalid input") from None
        except Exception as e:
            logger.error(
                "Failed to start workflow %s for app %s: %s",
                workflow_id,
                app_name,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Failed to start workflow"
            ) from None


__all__ = [
    "WORKFLOW_EXTRA_AVAILABLE",
    "StartRequest",
    "StartResult",
    "WorkflowStarter",
    "register_start_route",
    "starter_from_env",
]
