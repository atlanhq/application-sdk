"""The workflow lifecycle routes: start, stop, result and status.

``POST /workflows/v1/start``, ``POST /workflows/v1/stop/{workflow_id}/{run_id}``,
``GET /workflows/v1/result/{workflow_id}`` and
``GET /workflows/v1/status/{workflow_id}/{run_id}`` drive the app's workflows
through a Temporal client. The worker's handler service registers them through
:func:`register_workflow_routes`; a :class:`WorkflowRoutesSource` carries the
app, its workflow configuration and how to reach Temporal, so the routes can be
built and tested without the rest of the service.

Every route answers 503 while workflow execution is not configured, and acts
only on a run that belongs to this app (:func:`_describe_owned_workflow`): any
other ID answers the same 404 as a missing workflow.
"""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple
from uuid import uuid4

import temporalio.service
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel as PydanticBaseModel
from pydantic import ValidationError
from temporalio.client import (
    WorkflowExecutionDescription,
    WorkflowFailureError,
    WorkflowHandle,
)

from application_sdk._logging import get_logger
from application_sdk.app.entrypoint import _resolve_default_entrypoint
from application_sdk.app.registry import AppNotRegisteredError, AppRegistry
from application_sdk.common.dispatch import (
    canonical_workflow_type,
    resolve_dispatch_workflow_id,
)
from application_sdk.handler.request_contract import (
    RequestContractError,
    read_json_object,
    validate_request,
)
from application_sdk.handler.routes import _wrap_response
from application_sdk.observability.correlation import (
    CorrelationContext,
    set_correlation_context,
)

if TYPE_CHECKING:
    from temporalio.client import Client

    from application_sdk.app.entrypoint import EntryPointMetadata
    from application_sdk.handler.service import WorkflowClientConfig

__all__ = [
    "WorkflowRoutesSource",
    "register_workflow_routes",
]

# The worker's handler log name, kept from before the routes left
# handler/service.py so log queries and alerts keep matching.
logger = get_logger("application_sdk.handler.service")

#: The 503 ``/start`` answers while workflow execution is not configured.
_START_NOT_CONFIGURED = "Workflow execution not configured. Set ATLAN_TEMPORAL_HOST."
#: The 503 every other workflow route answers while it is not configured.
_NOT_CONFIGURED = "Workflow execution not configured."

_FAILED_STATUSES = frozenset({"FAILED", "TERMINATED", "CANCELED", "TIMED_OUT"})


@dataclass(frozen=True)
class WorkflowRoutesSource:
    """What the workflow routes need, for one app.

    Args:
        app_name: The app's name, for workflow IDs and logs.
        workflow_config: The workflow configuration captured when the routes
            are registered. A second service built in the same process must
            not change which app these routes act for.
        temporal_client: Returns the Temporal client, called once per request
            that needs it (so the worker can create it lazily).
        normalize: Turns a raw ``/start`` body into the shape the entry
            point's input validates.
    """

    app_name: str
    workflow_config: WorkflowClientConfig
    temporal_client: Callable[[], Awaitable[Client]]
    normalize: Callable[[dict[str, Any]], dict[str, Any]]


# ---------------------------------------------------------------------------
# Ownership, entry points and results
# ---------------------------------------------------------------------------


def _registered_workflow_types(
    workflow_config: WorkflowClientConfig,
) -> Mapping[str, EntryPointMetadata]:
    """Every Temporal workflow type the configured app registers, or an empty
    mapping when no app is configured or it is not in the registry."""
    if workflow_config.app_class is None or not workflow_config.app_name:
        return {}

    try:
        app_meta = AppRegistry.get_instance().get(workflow_config.app_name)
    except AppNotRegisteredError:
        logger.warning(
            "App %r not found in registry; no workflow types resolved",
            workflow_config.app_name,
            exc_info=True,
        )
        return {}
    return app_meta.workflow_types


def _output_type_for(
    workflow_config: WorkflowClientConfig, workflow_type_name: str
) -> type | None:
    """The declared output type of *workflow_type_name*, or ``None``.

    Looks the type up in the app's ``workflow_types`` index, the same index the
    worker registers from, so a ``legacy_workflow_types`` alias resolves as
    readily as a convention-derived name. ``None`` when the type is not one
    this app registers; the result is then read untyped.
    """
    ep = _registered_workflow_types(workflow_config).get(workflow_type_name)
    return ep.output_type if ep else None


def _workflow_not_found(workflow_id: str) -> HTTPException:
    """The 404 every workflow-lifecycle route answers for an ID it will not act on."""
    return HTTPException(status_code=404, detail=f"Workflow not found: {workflow_id}")


async def _describe_owned_workflow(
    handle: WorkflowHandle[Any, Any],
    workflow_config: WorkflowClientConfig,
    workflow_id: str,
) -> WorkflowExecutionDescription:
    """Describe *handle*, refusing a workflow that does not belong to this app.

    The Temporal namespace is shared across apps, so a bare workflow ID can
    name another app's run. A run is this app's only when both hold:

    - it is on ``workflow_config.task_queue``, where ``/start`` dispatches
      every workflow (``continue_as_new`` keeps the queue, and worker pools
      route activities, not workflows);
    - its type is one this app registers. Workflow types carry the app name,
      so this separates two apps that were configured onto the same queue.

    Any other run gets the same 404 as a missing workflow, so the route does
    not confirm that the ID exists elsewhere. Callers act on ``desc.run_id``
    (or a handle already pinned to a run) so that the run checked here is
    the run acted on.
    """
    desc = await handle.describe()
    if (
        not workflow_config.task_queue
        or desc.task_queue != workflow_config.task_queue
        or desc.workflow_type not in _registered_workflow_types(workflow_config)
    ):
        logger.warning(
            "Refusing workflow %s: type %s on task queue %s is not this app's (queue %s)",
            workflow_id,
            desc.workflow_type,
            desc.task_queue,
            workflow_config.task_queue,
        )
        raise _workflow_not_found(workflow_id)
    return desc


async def _get_workflow_result(
    client: Client,
    *,
    workflow_id: str,
    run_id: str,
    output_type: type | None,
) -> dict[str, Any]:
    """Fetch and normalise a completed workflow result.

    *run_id* pins the read to the run whose ownership the caller checked. A
    handle resolved by ID alone follows whichever run holds the ID now, which
    after a failed run can be a different app's workflow started under the
    same ID (FND-5215).

    Attempts typed deserialization first (so the OutputContract dataclass is
    reconstructed properly). Falls back to untyped if that fails, logging a
    warning so the mismatch is visible.

    ``result_type`` is a parameter of ``get_workflow_handle``, not of
    ``handle.result()`` — the Temporal SDK bakes the return type into the
    handle at construction time.

    Returns a plain dict suitable for JSON serialisation.
    """
    try:
        result = await client.get_workflow_handle(
            workflow_id, run_id=run_id, result_type=output_type
        ).result()
    except Exception:
        logger.warning(
            "Typed deserialization failed for workflow %s (output_type=%s), falling back to untyped result",
            workflow_id,
            output_type,
            exc_info=True,
        )
        result = await client.get_workflow_handle(workflow_id, run_id=run_id).result()

    if isinstance(result, PydanticBaseModel):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    return {}


def _resolve_app_entrypoint(
    app_name: str,
    selected_entrypoint: str | None,
    *,
    unknown_ep_status: int = 400,
    allow_workflow_type: bool = False,
) -> tuple[Any, Any]:
    """Look up the app in the registry and resolve the target entry point.

    Raises ``HTTPException`` for all failure modes so callers can tail-call
    this function without additional try/except.

    Args:
        app_name: Registered app name (from WorkflowClientConfig).
        selected_entrypoint: An entry-point selector, or ``None`` to use the
            default. Resolved as an entry-point name; a miss is a hard error
            unless ``allow_workflow_type`` widens the namespace. The order is
            never ambiguous — registration keeps names and types disjoint.
        unknown_ep_status: HTTP status to use when an explicitly named
            entrypoint does not exist.  ``400`` for /start (bad request),
            ``404`` for /input-contract (resource not found).
        allow_workflow_type: On a name miss, also try the selector against the
            app's registered Temporal workflow types (canonical and
            ``legacy_workflow_types`` aliases). Only the deprecated request-body
            ``workflow_type`` field passes ``True`` — that field exists for a
            legacy caller who genuinely holds a workflow type. The canonical
            ``?entrypoint=`` selector resolves entry-point names only, on every
            route, so an alias never becomes a second permanent name on the
            SDK's own HTTP surface.

    Returns:
        ``(app_meta, ep)`` — the :class:`AppMetadata` and the resolved
        :class:`EntryPointMetadata`.

    Raises:
        HTTPException 404: App not registered in the registry.
        HTTPException unknown_ep_status: Named entrypoint not found.
        HTTPException 400: Entrypoint required but not provided (multi-ep app
            with no marked default).
        HTTPException 500: App has no registered entry points at all.
    """
    try:
        app_meta = AppRegistry.get_instance().get(app_name)
    except AppNotRegisteredError:
        raise HTTPException(
            status_code=404, detail=f"App {app_name!r} not registered"
        ) from None

    entry_points = app_meta.entry_points

    if selected_entrypoint:
        if selected_entrypoint not in entry_points:
            by_workflow_type = (
                app_meta.workflow_types.get(selected_entrypoint)
                if allow_workflow_type
                else None
            )
            if by_workflow_type is not None:
                return app_meta, by_workflow_type
            logger.warning(
                "Unknown entrypoint '%s' for app %s; available: %s",
                selected_entrypoint,
                app_name,
                sorted(entry_points.keys()),
            )
            detail = (
                "Invalid entrypoint."
                if unknown_ep_status == 400
                else f"No input contract for entrypoint {selected_entrypoint!r}"
            )
            raise HTTPException(status_code=unknown_ep_status, detail=detail)
        return app_meta, entry_points[selected_entrypoint]

    ep = _resolve_default_entrypoint(entry_points)
    if ep is None:
        if entry_points:
            logger.warning(
                "entrypoint required but not provided for multi-entry-point "
                "app %s with no default; available: %s",
                app_name,
                sorted(entry_points.keys()),
            )
            raise HTTPException(
                status_code=400,
                detail="entrypoint is required for this app.",
            )
        raise HTTPException(
            status_code=500,
            detail="App has no registered entry points.",
        )

    return app_meta, ep


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


class _StartRequest(NamedTuple):
    """A ``/start`` body resolved to its entry point and validated input."""

    ep: Any
    workflow_name: str
    input_data: Any
    explicit_workflow_id: str | None


async def _parse_start_request(
    request: Request,
    app_name: str,
    workflow_config: WorkflowClientConfig,
    normalize: Callable[[dict[str, Any]], dict[str, Any]],
) -> _StartRequest:
    """Resolve and validate a ``/start`` body against its entry point's input type.

    Submission-time validation is ``/workflows/v1/validate`` (in
    :mod:`application_sdk.handler.metadata_routes`, served by the API host as
    well); this is ``/start``'s own last line, answering the same 422 shape.

    Raises ``HTTPException`` for a missing app class or an unknown entry point,
    and :class:`~application_sdk.handler.request_contract.RequestContractError`
    (answered as a 422 naming the field) when the body is not a JSON object or
    does not fit the entry point's input contract.
    """
    app_cls = workflow_config.app_class
    if app_cls is None:
        raise HTTPException(status_code=503, detail="App class not provided.")

    body = await read_json_object(request)
    explicit_workflow_id: str | None = body.pop("workflow_id", None)
    # ?entrypoint= query param is the canonical selector; fall back to the
    # legacy body field 'workflow_type' for backward compatibility.
    entrypoint_param: str | None = request.query_params.get("entrypoint")
    legacy_workflow_type: str | None = body.pop("workflow_type", None)
    selected_entrypoint: str | None = entrypoint_param or legacy_workflow_type

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

    # Migration note (BLDX-1354): On a multi-entry-point app that has no
    # explicit @entrypoint(default=True), omitting ?entrypoint= dispatches the
    # alphabetically-first entry point by name instead of returning 400.
    _, ep = _resolve_app_entrypoint(
        workflow_config.app_name,
        selected_entrypoint,
        allow_workflow_type=(
            entrypoint_param is None and legacy_workflow_type is not None
        ),
    )

    input_type = ep.input_type
    if input_type is None:
        raise HTTPException(
            status_code=500,
            detail=f"App class {app_cls.__name__} entry point has no input type.",
        )

    body = normalize(body)
    if "credentials" in body:
        # Always strip raw credentials — never pass through to Temporal
        # history. Credentials are resolved at runtime via DaprCredentialVault
        # using credential_guid.
        del body["credentials"]
        logger.debug("Stripped inline credentials from /start request body")

    # The body is the caller's workflow configuration: a misfit is the
    # caller's to fix and never succeeds on retry, so it answers 422 naming
    # the field (FND-501) rather than a 5xx.
    input_data = validate_request(input_type, body)
    return _StartRequest(
        ep=ep,
        workflow_name=canonical_workflow_type(workflow_config.app_name, ep),
        input_data=input_data,
        explicit_workflow_id=explicit_workflow_id,
    )


def _execution_timeout(workflow_config: WorkflowClientConfig) -> timedelta | None:
    """The SDK-level ceiling on a started workflow's run time, if one is set."""
    hours = workflow_config.workflow_max_timeout_hours
    return timedelta(hours=hours) if hours else None


async def _dispatch_start(
    source: WorkflowRoutesSource,
    client: Client,
    parsed: _StartRequest,
    workflow_id: str,
) -> JSONResponse:
    """Start the parsed workflow under *workflow_id* and answer its IDs.

    *workflow_id* is already stamped on the input by the shared dispatch
    helper. The correlation ID is the caller's when the input carries one,
    else a new UUID; it is bound to this request's log context and written to
    the run's memo.
    """
    input_data = parsed.input_data
    correlation_id = input_data.correlation_id or str(uuid4())
    input_data.correlation_id = correlation_id
    input_data._correlation_id = correlation_id

    corr_ctx = CorrelationContext(correlation_id=correlation_id)
    set_correlation_context(corr_ctx)

    config = source.workflow_config
    logger.info(
        "Starting workflow: app=%s workflow_id=%s queue=%s",
        source.app_name,
        workflow_id,
        config.task_queue,
    )
    handle = await client.start_workflow(
        parsed.workflow_name,
        args=[input_data],
        id=workflow_id,
        task_queue=config.task_queue,
        memo=corr_ctx.to_temporal_memo(),
        execution_timeout=_execution_timeout(config),
    )
    logger.info(
        "Workflow started: app=%s workflow_id=%s run_id=%s correlation_id=%s",
        source.app_name,
        handle.id,
        handle.result_run_id,
        correlation_id,
    )
    return JSONResponse(
        content={
            "success": True,
            "message": "Workflow started successfully",
            "data": {"workflow_id": handle.id, "run_id": handle.result_run_id},
            "correlation_id": correlation_id,
        }
    )


async def _start_workflow(
    source: WorkflowRoutesSource, request: Request
) -> JSONResponse:
    """``POST /workflows/v1/start``: validate the body, then start the workflow."""
    app_name = source.app_name
    workflow_id = "(unknown)"
    try:
        # Validated before the Temporal client is touched, so a malformed
        # configuration is rejected even when Temporal is unreachable.
        parsed = await _parse_start_request(
            request, app_name, source.workflow_config, source.normalize
        )
        workflow_id = parsed.explicit_workflow_id or workflow_id
        client = await source.temporal_client()
        # workflow_id is resolved and stamped by the shared dispatch helper,
        # the same one the executor backend uses, so the two dispatch paths
        # cannot drift. The caller's value (popped from the body before
        # validation) is used only when provided.
        workflow_id = resolve_dispatch_workflow_id(
            parsed.input_data,
            app_name,
            explicit_workflow_id=parsed.explicit_workflow_id or "",
        )
        return await _dispatch_start(source, client, parsed, workflow_id)
    except (HTTPException, RequestContractError):
        # A body that does not fit the entry point's input contract escapes
        # as RequestContractError, which the app-level handler answers as a
        # 422 naming the field.
        raise
    except ValidationError as e:
        # Any other ValidationError is raised past body validation — by the
        # SDK's own stamping or a model it builds — so it is an internal
        # failure, not a malformed request: a sanitized 500 with the real
        # cause in the log.
        logger.error(
            "Internal validation failure starting workflow for app %s: %s",
            app_name,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to start workflow"
        ) from None
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


# ---------------------------------------------------------------------------
# stop / result / status
# ---------------------------------------------------------------------------


@contextmanager
def _temporal_errors(
    workflow_id: str, detail: str, log_message: str, *log_args: object
) -> Iterator[None]:
    """Answer a failed Temporal call: not-found is 404, anything else a logged 500.

    An ``HTTPException`` raised inside (the ownership 404) passes unchanged.
    *log_message* gets *log_args* and then the exception's text.
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception as e:
        if (
            isinstance(e, temporalio.service.RPCError)
            and e.status == temporalio.service.RPCStatusCode.NOT_FOUND
        ):
            raise _workflow_not_found(workflow_id) from None
        # conformance: ignore[L009] boundary handler logs the real exception (exc_info) then raises a sanitized HTTPException `from None`; the log is the only server-side record of the actual failure.
        logger.error(log_message, *log_args, str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=detail) from None


async def _stop_workflow(
    source: WorkflowRoutesSource, workflow_id: str, run_id: str
) -> JSONResponse:
    """``POST /workflows/v1/stop/{workflow_id}/{run_id}``: terminate the run."""
    client = await source.temporal_client()
    with _temporal_errors(
        workflow_id,
        "Failed to stop workflow",
        "Failed to stop workflow %s run %s: %s",
        workflow_id,
        run_id,
    ):
        handle = client.get_workflow_handle(workflow_id, run_id=run_id)
        await _describe_owned_workflow(handle, source.workflow_config, workflow_id)
        await handle.terminate(reason="Stopped via handler API")
        return JSONResponse(
            content={"success": True, "message": "Workflow terminated successfully"}
        )


async def _completed_result_response(
    client: Client,
    workflow_config: WorkflowClientConfig,
    desc: WorkflowExecutionDescription,
    workflow_id: str,
) -> JSONResponse:
    """Read the run's result and answer it, or answer why it could not be read.

    The run's failure answers ``failed``; a result that cannot be decoded
    answers ``result_decode_failed`` with the error's type. Neither is an HTTP
    error: the request itself succeeded.
    """
    try:
        output_type = _output_type_for(workflow_config, desc.workflow_type)
        if output_type is None:
            logger.warning(
                "No output_type resolved for workflow_id=%s workflow_type=%s; using untyped deserialization",
                workflow_id,
                desc.workflow_type,
            )
        result_data = await _get_workflow_result(
            client,
            workflow_id=workflow_id,
            run_id=desc.run_id,
            output_type=output_type,
        )
    except WorkflowFailureError as exc:
        logger.warning(
            "Workflow execution failed for workflow_id=%s error_type=%s",
            workflow_id,
            type(exc).__name__,
            exc_info=True,
        )
        return JSONResponse(
            content=_wrap_response(
                {
                    "status": "failed",
                    "workflow_id": workflow_id,
                    "error": "Workflow execution failed.",
                },
                message="Workflow failed",
                success=False,
            )
        )
    except Exception as exc:
        logger.warning(
            "Workflow result decode failed for workflow_id=%s error_type=%s",
            workflow_id,
            type(exc).__name__,
            exc_info=True,
        )
        return JSONResponse(
            content=_wrap_response(
                {
                    "status": "result_decode_failed",
                    "workflow_id": workflow_id,
                    "error_type": type(exc).__name__,
                },
                message="Workflow result could not be decoded",
                success=False,
            )
        )
    return JSONResponse(
        content=_wrap_response(
            {"status": "completed", "workflow_id": workflow_id, "result": result_data},
            message="Workflow completed",
        )
    )


def _unfinished_result_response(workflow_id: str, status: str) -> JSONResponse:
    """The ``/result`` answer for a run that has no result to read."""
    if status == "RUNNING":
        data = {
            "status": "running",
            "workflow_id": workflow_id,
            "message": "Workflow is still running",
        }
        return JSONResponse(
            content=_wrap_response(data, message="Workflow is still running")
        )
    if status in _FAILED_STATUSES:
        data = {
            "status": "failed",
            "workflow_id": workflow_id,
            "error": f"Workflow {status.lower()}",
        }
        return JSONResponse(
            content=_wrap_response(data, message="Workflow failed", success=False)
        )
    data = {
        "status": status.lower(),
        "workflow_id": workflow_id,
        "message": f"Workflow status: {status}",
    }
    return JSONResponse(
        content=_wrap_response(data, message=f"Workflow status: {status}")
    )


async def _get_result(
    source: WorkflowRoutesSource, workflow_id: str, wait: bool
) -> JSONResponse:
    """``GET /workflows/v1/result/{workflow_id}``.

    With ``wait``, blocks until the run finishes and answers its result.
    Otherwise answers the result of a completed run, or the run's state.
    """
    client = await source.temporal_client()
    with _temporal_errors(
        workflow_id,
        "Failed to get workflow result",
        "Failed to get workflow result for %s: %s",
        workflow_id,
    ):
        handle = client.get_workflow_handle(workflow_id)
        desc = await _describe_owned_workflow(
            handle, source.workflow_config, workflow_id
        )
        status = desc.status.name if desc.status else "UNKNOWN"
        if wait or status == "COMPLETED":
            return await _completed_result_response(
                client, source.workflow_config, desc, workflow_id
            )
        return _unfinished_result_response(workflow_id, status)


def _execution_duration_seconds(desc: WorkflowExecutionDescription) -> int:
    """How long the run has executed: to its close, or to now while it is open."""
    if desc.execution_time and desc.close_time:
        return int((desc.close_time - desc.execution_time).total_seconds())
    if desc.execution_time:
        return int((datetime.now(UTC) - desc.execution_time).total_seconds())
    return 0


async def _get_status(
    source: WorkflowRoutesSource, workflow_id: str, run_id: str
) -> JSONResponse:
    """``GET /workflows/v1/status/{workflow_id}/{run_id}``."""
    client = await source.temporal_client()
    with _temporal_errors(
        workflow_id,
        "Failed to get workflow status",
        "Failed to get workflow status for %s run %s: %s",
        workflow_id,
        run_id,
    ):
        handle = client.get_workflow_handle(workflow_id, run_id=run_id)
        desc = await _describe_owned_workflow(
            handle, source.workflow_config, workflow_id
        )
        status = desc.status.name if desc.status else "UNKNOWN"
        return JSONResponse(
            content=_wrap_response(
                {
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "status": status,
                    "execution_duration_seconds": _execution_duration_seconds(desc),
                },
                message=f"Workflow status: {status}",
            )
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _require_configured(
    workflow_config: WorkflowClientConfig, detail: str = _NOT_CONFIGURED
) -> Callable[[], Awaitable[None]]:
    """A route dependency answering 503 while workflow execution is not configured."""

    async def configured() -> None:
        if not workflow_config.is_configured():
            raise HTTPException(status_code=503, detail=detail)

    return configured


def register_workflow_routes(app: FastAPI, source: WorkflowRoutesSource) -> None:
    """Register the workflow lifecycle routes (start, stop, result, status) on *app*."""
    configured = [Depends(_require_configured(source.workflow_config))]
    start_configured = [
        Depends(_require_configured(source.workflow_config, _START_NOT_CONFIGURED))
    ]

    @app.post("/workflows/v1/start", dependencies=start_configured)
    async def start_workflow(request: Request) -> JSONResponse:
        return await _start_workflow(source, request)

    @app.post("/workflows/v1/stop/{workflow_id}/{run_id:path}", dependencies=configured)
    async def stop_workflow(workflow_id: str, run_id: str) -> JSONResponse:
        return await _stop_workflow(source, workflow_id, run_id)

    @app.get("/workflows/v1/result/{workflow_id}", dependencies=configured)
    async def get_result(workflow_id: str, wait: bool = False) -> JSONResponse:
        return await _get_result(source, workflow_id, wait)

    @app.get(
        "/workflows/v1/status/{workflow_id}/{run_id:path}", dependencies=configured
    )
    async def get_workflow_status(workflow_id: str, run_id: str) -> JSONResponse:
        return await _get_status(source, workflow_id, run_id)
