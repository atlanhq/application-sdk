"""FastAPI service for per-app handler operations.

This module provides the FastAPI application that serves handler
operations for a single app. Each app runs its own handler service.

Routes:
    POST /workflows/v1/auth - Test authentication
    POST /workflows/v1/check - Run preflight checks
    POST /workflows/v1/metadata - Fetch metadata
    POST /workflows/v1/start - Start workflow execution
    POST /workflows/v1/stop/{workflow_id}/{run_id:path} - Stop workflow
    GET  /workflows/v1/result/{workflow_id} - Get workflow result
    GET  /workflows/v1/status/{workflow_id}/{run_id:path} - Get workflow status
    GET  /workflows/v1/config/{config_id} - Get workflow config
    POST /workflows/v1/config/{config_id} - Update workflow config
    POST /workflows/v1/file - Upload file to object storage
    GET  /health - Health check (k8s liveness probe)
    GET  /server/ready - Readiness probe

Usage::

    handler = MyHandler()
    app = create_app_handler_service(handler, app_name="my-app")
    uvicorn.run(app, host="0.0.0.0", port=8080)
"""

from __future__ import annotations

import dataclasses
import json
import mimetypes
from dataclasses import asdict
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger

from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    AuthInput,
    Credential,
    EventTriggerConfig,
    FileUploadResponse,
    MetadataInput,
    PreflightInput,
    SubscriptionConfig,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore
    from temporalio.client import Client
    from temporalio.converter import DataConverter

    from application_sdk.app.base import App
    from application_sdk.infrastructure.state import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Type alias for JSON-serializable values after dataclass conversion
JsonSerializable = str | int | float | bool | None | dict[str, Any] | list[Any]


def _convert_enums_recursive(obj: Any) -> JsonSerializable:
    """Recursively convert Enum values to their string values."""
    if isinstance(obj, dict):
        return {k: _convert_enums_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_enums_recursive(item) for item in obj]
    elif isinstance(obj, Enum):
        return cast(str, obj.value)
    return obj


def _serialize_output(output: Any) -> dict[str, JsonSerializable]:
    """Serialize an Output dataclass or dict to a JSON-compatible dict."""
    if isinstance(output, dict):
        return cast("dict[str, JsonSerializable]", _convert_enums_recursive(output))
    result = asdict(output)
    return cast("dict[str, JsonSerializable]", _convert_enums_recursive(result))


def _wrap_response(
    data: dict[str, JsonSerializable],
    *,
    message: str = "",
    success: bool = True,
) -> dict[str, JsonSerializable]:
    """Wrap response data in the standard envelope: {success, message, data}."""
    return {"success": success, "message": message, "data": data}


# ---------------------------------------------------------------------------
# Workflow client config and singleton
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class WorkflowClientConfig:
    """Configuration for the Temporal client used by the /start endpoint."""

    host: str = ""
    namespace: str = "default"
    task_queue: str = ""
    app_class: type[App] | None = dataclasses.field(default=None, repr=False)
    data_converter: DataConverter | None = dataclasses.field(default=None, repr=False)

    # TLS
    tls_enabled: bool = False
    tls_server_root_ca_cert_path: str = ""
    tls_client_cert_path: str = ""
    tls_client_private_key_path: str = ""
    tls_domain: str = ""

    # Auth
    auth_enabled: bool = False
    auth_client_id: str = ""
    auth_client_secret: str = ""
    auth_token_url: str = ""
    auth_base_url: str = ""
    auth_scopes: str = ""

    def is_configured(self) -> bool:
        return bool(self.host and self.app_class)


_temporal_client: Client | None = None
_workflow_config: WorkflowClientConfig = WorkflowClientConfig()
_handler_auth_manager: Any | None = None
_state_store: StateStore | None = None
_storage: ObjectStore | None = None

# Directory where generated contract JSON files are stored
CONTRACT_GENERATED_DIR = Path.cwd() / "contract" / "generated"


async def _get_temporal_client() -> Client:
    """Get or lazily create the singleton Temporal client."""
    global _temporal_client, _handler_auth_manager

    if _temporal_client is not None:
        return _temporal_client

    from application_sdk.execution import create_temporal_client

    api_key: str | None = None
    if _workflow_config.auth_enabled:
        from application_sdk.execution import TemporalAuthConfig, TemporalAuthManager

        auth_config = TemporalAuthConfig(
            client_id=_workflow_config.auth_client_id,
            client_secret=_workflow_config.auth_client_secret,
            token_url=_workflow_config.auth_token_url,
            base_url=_workflow_config.auth_base_url,
            scopes=_workflow_config.auth_scopes,
        )
        _handler_auth_manager = TemporalAuthManager(auth_config)
        api_key = await _handler_auth_manager.acquire_initial_token()
        logger.info("Acquired auth token for handler Temporal client")

    logger.info(
        "Connecting to Temporal for workflow execution",
        host=_workflow_config.host,
        namespace=_workflow_config.namespace,
        tls_enabled=_workflow_config.tls_enabled,
        auth_enabled=_workflow_config.auth_enabled,
    )

    _temporal_client = await create_temporal_client(
        _workflow_config.host,
        _workflow_config.namespace,
        data_converter=_workflow_config.data_converter,
        api_key=api_key,
        tls_enabled=_workflow_config.tls_enabled,
        tls_server_root_ca_cert_path=_workflow_config.tls_server_root_ca_cert_path,
        tls_client_cert_path=_workflow_config.tls_client_cert_path,
        tls_client_private_key_path=_workflow_config.tls_client_private_key_path,
        tls_domain=_workflow_config.tls_domain,
    )
    logger.info("Connected to Temporal")

    if _handler_auth_manager is not None:
        _handler_auth_manager.start_background_refresh(_temporal_client)
        logger.info("Background token refresh started for handler")

    return _temporal_client


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------


def create_app_handler_service(
    handler: Handler,
    *,
    app_name: str = "",
    app_class: type[App] | None = None,
    temporal_host: str = "",
    temporal_namespace: str = "default",
    task_queue: str = "",
    data_converter: DataConverter | None = None,
    tls_enabled: bool = False,
    tls_server_root_ca_cert_path: str = "",
    tls_client_cert_path: str = "",
    tls_client_private_key_path: str = "",
    tls_domain: str = "",
    auth_enabled: bool = False,
    auth_client_id: str = "",
    auth_client_secret: str = "",
    auth_token_url: str = "",
    auth_base_url: str = "",
    auth_scopes: str = "",
    state_store: StateStore | None = None,
    storage: ObjectStore | None = None,
    event_triggers: list[EventTriggerConfig] | None = None,
    subscriptions: list[SubscriptionConfig] | None = None,
    title: str = "Handler Service",
    description: str = "Per-app handler service for authentication, preflight, and metadata operations",
    version: str = "1.0.0",
) -> FastAPI:
    """Create a FastAPI app for a single handler.

    Args:
        handler: The Handler instance to serve.
        app_name: App name for logging context.
        app_class: App class for workflow execution (enables /start, /stop, etc.).
        temporal_host: Temporal server address (e.g., "localhost:7233").
        temporal_namespace: Temporal namespace.
        task_queue: Task queue name (default: "{app_name}-queue").
        data_converter: Optional custom Temporal DataConverter.
        state_store: Optional state store for workflow config persistence.
        storage: Optional obstore store for file uploads.
        title: OpenAPI title.
        description: OpenAPI description.
        version: API version string.

    Returns:
        Configured FastAPI application.
    """
    global _workflow_config, _state_store, _storage

    _state_store = state_store
    _storage = storage
    _workflow_config = WorkflowClientConfig(
        host=temporal_host,
        namespace=temporal_namespace,
        task_queue=task_queue or f"{app_name}-queue",
        app_class=app_class,
        data_converter=data_converter,
        tls_enabled=tls_enabled,
        tls_server_root_ca_cert_path=tls_server_root_ca_cert_path,
        tls_client_cert_path=tls_client_cert_path,
        tls_client_private_key_path=tls_client_private_key_path,
        tls_domain=tls_domain,
        auth_enabled=auth_enabled,
        auth_client_id=auth_client_id,
        auth_client_secret=auth_client_secret,
        auth_token_url=auth_token_url,
        auth_base_url=auth_base_url,
        auth_scopes=auth_scopes,
    )

    from application_sdk.constants import ENABLE_MCP

    if ENABLE_MCP and app_name:
        from contextlib import asynccontextmanager

        from application_sdk.server.mcp import MCPServer

        _mcp_server = MCPServer(application_name=app_name)

        @asynccontextmanager
        async def _mcp_lifespan(fastapi_app: FastAPI):  # type: ignore[type-arg]
            await _mcp_server.register_tools_from_registry(app_name)
            mcp_http = _mcp_server.server.http_app()
            async with mcp_http.lifespan(mcp_http):
                fastapi_app.mount("", mcp_http)
                yield

        app = FastAPI(
            title=title,
            description=description,
            version=version,
            lifespan=_mcp_lifespan,
        )
    else:
        app = FastAPI(title=title, description=description, version=version)

    from application_sdk.server.middleware import LogMiddleware, MetricsMiddleware

    app.add_middleware(MetricsMiddleware)
    app.add_middleware(LogMiddleware)

    def _create_context(credentials: list[Credential]) -> HandlerContext:
        from application_sdk.infrastructure.context import get_infrastructure

        infra = get_infrastructure()
        return HandlerContext(
            app_name=app_name,
            request_id=uuid4(),
            started_at=datetime.now(UTC),
            _credentials=credentials,
            _secret_store=infra.secret_store if infra else None,
            _state_store=infra.state_store if infra else None,
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/auth")
    async def test_auth(request: Request) -> JSONResponse:
        body: dict[str, Any] = await request.json()
        credentials = [
            Credential(key=c["key"], value=c["value"])
            for c in body.get("credentials", [])
        ]
        context = _create_context(credentials)
        handler._context = context

        try:
            auth_input = AuthInput(
                credentials=credentials,
                connection_id=body.get("connection_id", ""),
                timeout_seconds=body.get("timeout_seconds", 30),
            )
            logger.info(
                "Auth test started",
                app_name=app_name,
                request_id=context.request_id_str,
            )
            result = await handler.test_auth(auth_input)
            logger.info(
                "Auth test completed",
                app_name=app_name,
                request_id=context.request_id_str,
                status=result.status.value,
            )
            return JSONResponse(
                content=_wrap_response(
                    _serialize_output(result),
                    message=result.message or f"Authentication {result.status.value}",
                )
            )
        except HandlerError as e:
            logger.error(
                "Auth test failed",
                app_name=app_name,
                request_id=context.request_id_str,
                error=str(e),
            )
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except Exception as e:
            logger.error(
                "Auth test failed unexpectedly",
                app_name=app_name,
                request_id=context.request_id_str,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise HTTPException(
                status_code=500, detail=f"Internal error: {e}"
            ) from None
        finally:
            handler._context = None

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/check")
    async def preflight_check(request: Request) -> JSONResponse:
        body = await request.json()
        credentials = [
            Credential(key=c["key"], value=c["value"])
            for c in body.get("credentials", [])
        ]
        context = _create_context(credentials)
        handler._context = context

        try:
            preflight_input = PreflightInput(
                credentials=credentials,
                connection_config=body.get("connection_config", {}),
                checks_to_run=body.get("checks_to_run", []),
                timeout_seconds=body.get("timeout_seconds", 60),
            )
            logger.info(
                "Preflight check started",
                app_name=app_name,
                request_id=context.request_id_str,
            )
            result = await handler.preflight_check(preflight_input)
            logger.info(
                "Preflight check completed",
                app_name=app_name,
                request_id=context.request_id_str,
                status=result.status.value,
                checks_count=len(result.checks),
            )
            return JSONResponse(
                content=_wrap_response(
                    _serialize_output(result),
                    message=result.message or f"Preflight check {result.status.value}",
                )
            )
        except HandlerError as e:
            logger.error(
                "Preflight check failed",
                app_name=app_name,
                request_id=context.request_id_str,
                error=str(e),
            )
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except Exception as e:
            logger.error(
                "Preflight check failed unexpectedly",
                app_name=app_name,
                request_id=context.request_id_str,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise HTTPException(
                status_code=500, detail=f"Internal error: {e}"
            ) from None
        finally:
            handler._context = None

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/metadata")
    async def fetch_metadata(request: Request) -> JSONResponse:
        body = await request.json()
        credentials = [
            Credential(key=c["key"], value=c["value"])
            for c in body.get("credentials", [])
        ]
        context = _create_context(credentials)
        handler._context = context

        try:
            metadata_input = MetadataInput(
                credentials=credentials,
                connection_config=body.get("connection_config", {}),
                object_filter=body.get("object_filter", ""),
                include_fields=body.get("include_fields", True),
                max_objects=body.get("max_objects", 1000),
                timeout_seconds=body.get("timeout_seconds", 120),
            )
            logger.info(
                "Metadata fetch started",
                app_name=app_name,
                request_id=context.request_id_str,
            )
            result = await handler.fetch_metadata(metadata_input)
            logger.info(
                "Metadata fetch completed",
                app_name=app_name,
                request_id=context.request_id_str,
                object_count=len(result.objects),
                truncated=result.truncated,
            )
            return JSONResponse(
                content=_wrap_response(
                    _serialize_output(result),
                    message=f"Fetched {result.total_count} objects",
                )
            )
        except HandlerError as e:
            logger.error(
                "Metadata fetch failed",
                app_name=app_name,
                request_id=context.request_id_str,
                error=str(e),
            )
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except Exception as e:
            logger.error(
                "Metadata fetch failed unexpectedly",
                app_name=app_name,
                request_id=context.request_id_str,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise HTTPException(
                status_code=500, detail=f"Internal error: {e}"
            ) from None
        finally:
            handler._context = None

    # ------------------------------------------------------------------
    # Workflow lifecycle
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/start")
    async def start_workflow(request: Request) -> JSONResponse:
        if not _workflow_config.is_configured():
            raise HTTPException(
                status_code=503,
                detail="Workflow execution not configured. Set ATLAN_TEMPORAL_HOST.",
            )

        app_cls = _workflow_config.app_class
        if app_cls is None:
            raise HTTPException(status_code=503, detail="App class not provided.")

        body: dict[str, Any] = await request.json()
        explicit_workflow_id: str | None = body.pop("workflow_id", None)
        workflow_id = explicit_workflow_id or "(unknown)"

        try:
            client = await _get_temporal_client()

            input_type = getattr(app_cls, "_input_type", None)
            if input_type is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"App class {app_cls.__name__} does not define _input_type.",
                )

            input_data = input_type(**body)

            if explicit_workflow_id:
                workflow_id = explicit_workflow_id
            else:
                config_hash = input_data.config_hash()
                workflow_id = f"{app_name}-{config_hash}-{uuid4().hex[:8]}"

            correlation_id = str(uuid4())
            input_data._correlation_id = correlation_id

            from application_sdk.observability.correlation import (
                CorrelationContext,
                set_correlation_context,
            )

            set_correlation_context(CorrelationContext(correlation_id=correlation_id))
            corr_ctx = CorrelationContext(correlation_id=correlation_id)

            logger.info(
                "Starting workflow",
                app_name=app_name,
                workflow_id=workflow_id,
                task_queue=_workflow_config.task_queue,
            )

            handle = await client.start_workflow(
                app_cls._app_name,  # type: ignore[attr-defined]
                args=[input_data],
                id=workflow_id,
                task_queue=_workflow_config.task_queue,
                memo=corr_ctx.to_temporal_memo(),
            )

            logger.info(
                "Workflow started",
                app_name=app_name,
                workflow_id=handle.id,
                run_id=handle.result_run_id,
                correlation_id=correlation_id,
            )

            if _state_store is not None:
                try:
                    config_to_store = {
                        k: v for k, v in body.items() if k != "credentials"
                    }
                    config_to_store["workflow_id"] = handle.id
                    await _state_store.save(f"workflows/{handle.id}", config_to_store)
                except Exception:
                    logger.warning(
                        "Failed to save workflow config to state store",
                        workflow_id=handle.id,
                        exc_info=True,
                    )

            return JSONResponse(
                content={
                    "success": True,
                    "message": "Workflow started successfully",
                    "data": {"workflow_id": handle.id, "run_id": handle.result_run_id},
                    "correlation_id": correlation_id,
                }
            )

        except TypeError as e:
            logger.error("Invalid workflow input", app_name=app_name, error=str(e))
            raise HTTPException(status_code=400, detail=f"Invalid input: {e}") from None
        except Exception as e:
            logger.error(
                "Failed to start workflow",
                app_name=app_name,
                workflow_id=workflow_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to start workflow: {e}"
            ) from None

    @app.post("/workflows/v1/stop/{workflow_id}/{run_id:path}")
    async def stop_workflow(workflow_id: str, run_id: str) -> JSONResponse:
        if not _workflow_config.is_configured():
            raise HTTPException(
                status_code=503, detail="Workflow execution not configured."
            )

        client = await _get_temporal_client()
        try:
            handle = client.get_workflow_handle(workflow_id, run_id=run_id)
            await handle.terminate(reason="Stopped via handler API")
            return JSONResponse(
                content={"success": True, "message": "Workflow terminated successfully"}
            )
        except Exception as e:
            error_msg = str(e)
            if "not found" in error_msg.lower():
                raise HTTPException(
                    status_code=404, detail=f"Workflow not found: {workflow_id}"
                ) from None
            logger.error(
                "Failed to stop workflow",
                workflow_id=workflow_id,
                run_id=run_id,
                error=error_msg,
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to stop workflow: {error_msg}"
            ) from None

    @app.get("/workflows/v1/result/{workflow_id}")
    async def get_result(workflow_id: str, wait: bool = False) -> JSONResponse:
        if not _workflow_config.is_configured():
            raise HTTPException(
                status_code=503, detail="Workflow execution not configured."
            )

        client = await _get_temporal_client()
        try:
            handle = client.get_workflow_handle(workflow_id)

            if wait:
                try:
                    result = await handle.result()
                    return JSONResponse(
                        content=_wrap_response(
                            {
                                "status": "completed",
                                "workflow_id": workflow_id,
                                "result": _serialize_output(result),
                            },
                            message="Workflow completed",
                        )
                    )
                except Exception as e:
                    return JSONResponse(
                        content=_wrap_response(
                            {
                                "status": "failed",
                                "workflow_id": workflow_id,
                                "error": str(e),
                            },
                            message="Workflow failed",
                        )
                    )

            desc = await handle.describe()
            status = desc.status.name if desc.status else "UNKNOWN"

            if status == "RUNNING":
                return JSONResponse(
                    content=_wrap_response(
                        {
                            "status": "running",
                            "workflow_id": workflow_id,
                            "message": "Workflow is still running",
                        },
                        message="Workflow is still running",
                    )
                )
            elif status == "COMPLETED":
                try:
                    result = await handle.result()
                    return JSONResponse(
                        content=_wrap_response(
                            {
                                "status": "completed",
                                "workflow_id": workflow_id,
                                "result": _serialize_output(result),
                            },
                            message="Workflow completed",
                        )
                    )
                except Exception as e:
                    return JSONResponse(
                        content=_wrap_response(
                            {
                                "status": "failed",
                                "workflow_id": workflow_id,
                                "error": str(e),
                            },
                            message="Workflow failed",
                        )
                    )
            elif status in ("FAILED", "TERMINATED", "CANCELED", "TIMED_OUT"):
                return JSONResponse(
                    content=_wrap_response(
                        {
                            "status": "failed",
                            "workflow_id": workflow_id,
                            "error": f"Workflow {status.lower()}",
                        },
                        message="Workflow failed",
                    )
                )
            else:
                return JSONResponse(
                    content=_wrap_response(
                        {
                            "status": status.lower(),
                            "workflow_id": workflow_id,
                            "message": f"Workflow status: {status}",
                        },
                        message=f"Workflow status: {status}",
                    )
                )

        except Exception as e:
            error_msg = str(e)
            if "not found" in error_msg.lower():
                raise HTTPException(
                    status_code=404, detail=f"Workflow not found: {workflow_id}"
                ) from None
            logger.error(
                "Failed to get workflow result",
                workflow_id=workflow_id,
                error=error_msg,
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to get workflow result: {error_msg}"
            ) from None

    @app.get("/workflows/v1/status/{workflow_id}/{run_id:path}")
    async def get_workflow_status(workflow_id: str, run_id: str) -> JSONResponse:
        if not _workflow_config.is_configured():
            raise HTTPException(
                status_code=503, detail="Workflow execution not configured."
            )

        client = await _get_temporal_client()
        try:
            handle = client.get_workflow_handle(workflow_id, run_id=run_id)
            desc = await handle.describe()
            status = desc.status.name if desc.status else "UNKNOWN"

            execution_duration_seconds = 0
            if desc.execution_time and desc.close_time:
                execution_duration_seconds = int(
                    (desc.close_time - desc.execution_time).total_seconds()
                )
            elif desc.execution_time:
                execution_duration_seconds = int(
                    (datetime.now(UTC) - desc.execution_time).total_seconds()
                )

            return JSONResponse(
                content=_wrap_response(
                    {
                        "workflow_id": workflow_id,
                        "run_id": run_id,
                        "status": status,
                        "execution_duration_seconds": execution_duration_seconds,
                    },
                    message=f"Workflow status: {status}",
                )
            )
        except Exception as e:
            error_msg = str(e)
            if "not found" in error_msg.lower():
                raise HTTPException(
                    status_code=404, detail=f"Workflow not found: {workflow_id}"
                ) from None
            logger.error(
                "Failed to get workflow status",
                workflow_id=workflow_id,
                run_id=run_id,
                error=error_msg,
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to get workflow status: {error_msg}"
            ) from None

    # ------------------------------------------------------------------
    # Config (state store)
    # ------------------------------------------------------------------

    @app.get("/workflows/v1/config/{config_id}")
    async def get_workflow_config(config_id: str) -> JSONResponse:
        if _state_store is None:
            raise HTTPException(status_code=503, detail="State store not configured")
        config = await _state_store.load(f"workflows/{config_id}")
        if config is None:
            raise HTTPException(
                status_code=404, detail=f"Config not found: {config_id}"
            )
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, JsonSerializable]", config),
                message="Workflow configuration fetched successfully",
            )
        )

    @app.post("/workflows/v1/config/{config_id}")
    async def update_workflow_config(config_id: str, request: Request) -> JSONResponse:
        if _state_store is None:
            raise HTTPException(status_code=503, detail="State store not configured")
        body = await request.json()
        await _state_store.save(f"workflows/{config_id}", body)
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, JsonSerializable]", body),
                message="Workflow configuration updated successfully",
            )
        )

    # ------------------------------------------------------------------
    # File upload
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/file")
    async def upload_file(
        file: UploadFile = File(...),
        filename: str | None = Form(None),
        prefix: str | None = Form(None),
        contentType: str | None = Form(None),
    ) -> JSONResponse:
        if _storage is None:
            raise HTTPException(status_code=503, detail="Storage not configured")

        import asyncio
        import os
        import shutil
        import tempfile
        from pathlib import PurePosixPath

        from application_sdk.storage.ops import upload_file as _upload_file

        raw_name = filename or file.filename or "upload"
        extension = PurePosixPath(raw_name).suffix.lstrip(".")
        resolved_type = (
            contentType
            or file.content_type
            or (mimetypes.guess_type(raw_name)[0] if raw_name else None)
            or "application/octet-stream"
        )

        file_id = str(uuid4())
        effective_prefix = prefix or "workflow_file_upload"
        stored_name = f"{file_id}.{extension}" if extension else file_id
        key = f"{effective_prefix}/{stored_name}"

        # Stream the upload: drain the spooled temp file to a named temp file,
        # then stream-upload to the object store (avoids materialising in memory).
        fd, tmp_path = tempfile.mkstemp(suffix=f".{extension}" if extension else "")
        try:
            os.close(fd)

            def _drain_to_tmp() -> int:
                with open(tmp_path, "wb") as dst:
                    shutil.copyfileobj(file.file, dst)
                return os.path.getsize(tmp_path)

            file_size = await asyncio.to_thread(_drain_to_tmp)
            await _upload_file(key, tmp_path, _storage)
        finally:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        response_obj = FileUploadResponse(
            id=file_id,
            version="1",
            is_active=True,
            created_at=now_ms,
            updated_at=now_ms,
            file_name=stored_name,
            raw_name=raw_name,
            key=key,
            extension=extension,
            content_type=resolved_type,
            file_size=file_size,
            is_uploaded=True,
            uploaded_at=datetime.now(UTC).isoformat(),
        )
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, JsonSerializable]", response_obj.to_wire_dict()),
                message="File uploaded successfully",
            )
        )

    # ------------------------------------------------------------------
    # Dapr pub/sub and event-triggered workflow endpoints
    # ------------------------------------------------------------------

    _event_triggers: list[EventTriggerConfig] = list(event_triggers or [])
    _subscriptions: list[SubscriptionConfig] = list(subscriptions or [])

    @app.get("/dapr/subscribe")
    async def get_dapr_subscriptions() -> JSONResponse:
        from application_sdk.constants import EVENT_STORE_NAME

        result: list[dict[str, Any]] = []

        for sub in _subscriptions:
            sub_dict: dict[str, Any] = {
                "pubsubname": sub.component_name,
                "topic": sub.topic,
                "route": f"/subscriptions/v1/{sub.route}",
            }
            if sub.bulk_enabled:
                sub_dict["bulkSubscribe"] = {
                    "enabled": sub.bulk_enabled,
                    "maxMessagesCount": sub.bulk_max_messages,
                    "maxAwaitDurationMs": sub.bulk_max_await_ms,
                }
            if sub.dead_letter_topic:
                sub_dict["deadLetterTopic"] = sub.dead_letter_topic
            result.append(sub_dict)

        for trigger in _event_triggers:
            filters = [
                f"({f.path} {f.operator} '{f.value}')" for f in trigger.event_filters
            ]
            filters.append(f"event.data.event_name == '{trigger.event_name}'")
            filters.append(f"event.data.event_type == '{trigger.event_type}'")

            result.append(
                {
                    "pubsubname": EVENT_STORE_NAME,
                    "topic": trigger.event_type,
                    "routes": {
                        "rules": [
                            {
                                "match": " && ".join(filters),
                                "path": f"/events/v1/event/{trigger.event_id}",
                            }
                        ],
                        "default": "/events/v1/drop",
                    },
                }
            )

        return JSONResponse(content=result)

    @app.post("/events/v1/event/{event_id}")
    async def handle_event(event_id: str, request: Request) -> JSONResponse:
        if not _workflow_config.is_configured():
            raise HTTPException(
                status_code=503, detail="Workflow execution not configured."
            )

        trigger = next((t for t in _event_triggers if t.event_id == event_id), None)
        if trigger is None:
            raise HTTPException(
                status_code=404,
                detail=f"No trigger registered for event_id: {event_id}",
            )

        app_cls = _workflow_config.app_class
        if app_cls is None:
            raise HTTPException(status_code=503, detail="App class not provided.")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from None

        try:
            client = await _get_temporal_client()

            input_type = getattr(app_cls, "_input_type", None)
            if input_type is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"App class {app_cls.__name__} does not define _input_type.",
                )

            event_data: dict[str, Any] = body.get("data", body)
            input_data = (
                input_type(**event_data)
                if isinstance(event_data, dict)
                else input_type()
            )

            workflow_id = f"{app_name}-event-{event_id}-{uuid4().hex[:8]}"

            handle = await client.start_workflow(
                app_cls._app_name,  # type: ignore[attr-defined]
                args=[input_data],
                id=workflow_id,
                task_queue=_workflow_config.task_queue,
            )
            return JSONResponse(
                content={
                    "status": "SUCCESS",
                    "workflow_id": handle.id,
                    "run_id": handle.result_run_id,
                }
            )
        except Exception as e:
            logger.error(
                "Failed to start event-triggered workflow",
                event_id=event_id,
                error=str(e),
            )
            return JSONResponse(content={"status": "RETRY"}, status_code=500)

    @app.post("/events/v1/drop")
    async def drop_event() -> JSONResponse:
        return JSONResponse(content={"success": False, "status": "DROP"})

    # Register dynamic subscription routes
    for _sub in _subscriptions:
        _sub_route = f"/subscriptions/v1/{_sub.route}"
        app.add_api_route(_sub_route, _sub.handler, methods=["POST"])

    # ------------------------------------------------------------------
    # ConfigMap endpoints
    # ------------------------------------------------------------------

    @app.get("/workflows/v1/configmap/{config_map_id}")
    async def get_configmap(config_map_id: str) -> JSONResponse:
        if CONTRACT_GENERATED_DIR.exists():
            for json_file in CONTRACT_GENERATED_DIR.rglob("*.json"):
                if json_file.stem == config_map_id:
                    with open(json_file) as f:
                        raw = json.load(f)
                    configmap = {
                        "kind": "ConfigMap",
                        "apiVersion": "v1",
                        "metadata": {"name": config_map_id},
                        "data": {"config": json.dumps(raw.get("config", raw))},
                    }
                    return JSONResponse(
                        content=_wrap_response(
                            cast("dict[str, JsonSerializable]", configmap),
                            message="ConfigMap fetched successfully",
                        )
                    )
        return JSONResponse(content=_wrap_response({}, message="ConfigMap not found"))

    @app.get("/workflows/v1/configmaps")
    async def list_configmaps() -> JSONResponse:
        configmap_ids: list[str] = []
        if CONTRACT_GENERATED_DIR.exists():
            for json_file in CONTRACT_GENERATED_DIR.glob("*.json"):
                if json_file.stem != "manifest":
                    configmap_ids.append(json_file.stem)
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, JsonSerializable]", {"configmaps": configmap_ids}),
                message="ConfigMaps listed successfully",
            )
        )

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/server/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}

    return app


def run_app_handler_service(
    handler: Handler,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    log_level: str = "info",
    **kwargs: Any,
) -> None:
    """Create and run the handler service with uvicorn.

    Convenience wrapper around ``create_app_handler_service()`` that blocks
    until the server is stopped. All keyword arguments are forwarded to
    ``create_app_handler_service()``.

    Args:
        handler: The Handler instance to serve.
        host: Bind host (default: "0.0.0.0").
        port: Bind port (default: 8080).
        log_level: Uvicorn log level (default: "info").
        **kwargs: Additional keyword arguments forwarded to
            ``create_app_handler_service()``.
    """
    import uvicorn

    app = create_app_handler_service(handler, **kwargs)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
