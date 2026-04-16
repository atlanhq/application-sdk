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
    GET  /metrics - Prometheus metrics endpoint (when enabled)
    GET  / - Serve frontend UI (app/generated/frontend/static/index.html)

Usage::

    handler = MyHandler()
    app = create_app_handler_service(handler, app_name="my-app")
    uvicorn.run(app, host="0.0.0.0", port=8000)
"""

from __future__ import annotations

import dataclasses
import json
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel as PydanticBaseModel

from application_sdk.constants import CONTRACT_GENERATED_DIR as _CONTRACT_GENERATED_DIR
from application_sdk.constants import DEPLOYMENT_NAME, ENABLE_PROMETHEUS_METRICS
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    AuthInput,
    AuthStatus,
    EventTriggerConfig,
    FileUploadResponse,
    HandlerCredential,
    MetadataInput,
    PreflightInput,
    PreflightStatus,
    SubscriptionConfig,
)
from application_sdk.handler.manifest import AppManifest
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def _serialize_credential_value(v: Any) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v)


_CREDENTIAL_KEYS = frozenset(
    {
        "host",
        "port",
        "authType",
        "username",
        "password",
        "connectorType",
        "connectorConfigName",
        "extra",
    }
)


def _flatten_to_pairs(creds_dict: dict[str, Any]) -> list[dict[str, str]]:
    """Convert a flat credential dict to v3 [{key, value}] pairs."""
    pairs: list[dict[str, str]] = []
    extra = creds_dict.pop("extra", None)
    for k, v in creds_dict.items():
        if v is not None:
            pairs.append({"key": k, "value": _serialize_credential_value(v)})
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is not None:
                pairs.append(
                    {"key": f"extra.{k}", "value": _serialize_credential_value(v)}
                )
    return pairs


def _pairs_to_flat(pairs: list[dict[str, str]]) -> dict[str, Any]:
    """Convert v3 [{key, value}] pairs to a flat credential dict.

    Reverse of ``_flatten_to_pairs``.  ``extra.*`` keys are nested under
    an ``extra`` dict so ``parse_credentials_extra()`` works correctly.

    Note: all values remain strings — no type coercion is performed.
    A round-trip through ``_flatten_to_pairs`` then ``_pairs_to_flat``
    will stringify non-string values (e.g. ``int 5432`` → ``str "5432"``).
    """
    flat: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for p in pairs:
        key, value = p["key"], p["value"]
        if key.startswith("extra."):
            extra[key[len("extra.") :]] = value
        else:
            flat[key] = value
    if extra:
        flat["extra"] = extra
    return flat


def _normalize_credentials(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize v2 credential formats to v3 list[{key, value}] format.

    Handles three formats:

    1. v3 array (already correct):
        {"credentials": [{"key": "host", "value": "..."}]}

    2. v2 nested dict (Heracles internal):
        {"credentials": {"host": "...", "username": "...", "extra": {...}}}

    3. v2 flat top-level (Heracles credential test):
        {"host": "...", "authType": "basic", "password": "...", "extra": {...}}

    Returns the body with credentials normalized to v3 array format.
    """
    creds = body.get("credentials")

    # Already v3 array format — no conversion needed
    if isinstance(creds, list):
        return body

    # Format 2: nested dict under "credentials" key
    if isinstance(creds, dict):
        logger.info(
            "Converting v2 nested-dict credentials to v3 list, keys=%s",
            list(creds.keys()),
        )
        return {**body, "credentials": _flatten_to_pairs(dict(creds))}

    # Format 3: flat top-level keys (detect by presence of known credential keys)
    if creds is None and _CREDENTIAL_KEYS & body.keys():
        flat_creds = {k: body[k] for k in list(body.keys()) if k in _CREDENTIAL_KEYS}
        logger.info(
            "Converting v2 flat top-level credentials to v3 list, keys=%s",
            list(flat_creds.keys()),
        )
        return {**body, "credentials": _flatten_to_pairs(flat_creds)}

    return body


if TYPE_CHECKING:
    from obstore.store import ObjectStore
    from temporalio.client import Client
    from temporalio.converter import DataConverter

    from application_sdk.app.base import App
    from application_sdk.infrastructure.secrets import SecretStore
    from application_sdk.infrastructure.state import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap_response(
    data: dict[str, Any] | list[Any],
    *,
    message: str = "",
    success: bool = True,
) -> dict[str, Any]:
    """Wrap response data in the standard envelope: {success, message, data}."""
    return {"success": success, "message": message, "data": data}


async def _get_workflow_result(
    client: "Client",
    *,
    workflow_id: str,
    output_type: type | None,
) -> dict[str, Any]:
    """Fetch and normalise a completed workflow result.

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
            workflow_id, result_type=output_type
        ).result()
    except Exception:
        logger.warning(
            "Typed deserialization failed for workflow %s (output_type=%s), falling back to untyped result",
            workflow_id,
            output_type,
            exc_info=True,
        )
        result = await client.get_workflow_handle(workflow_id).result()

    if isinstance(result, PydanticBaseModel):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    return {}


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
_secret_store: SecretStore | None = None
_storage: ObjectStore | None = None

# Directory where generated contract JSON files are stored
CONTRACT_GENERATED_DIR = Path(_CONTRACT_GENERATED_DIR)


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
    secret_store: SecretStore | None = None,
    storage: ObjectStore | None = None,
    event_triggers: list[EventTriggerConfig] | None = None,
    subscriptions: list[SubscriptionConfig] | None = None,
    manifest: AppManifest | None = None,
    title: str = "Handler Service",
    description: str = "Per-app handler service for authentication, preflight, and metadata operations",
    version: str = "1.0.0",
    frontend_assets_path: str = "app/generated/frontend/static",
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
        secret_store: Optional secret store for credential interception.
            When provided, the ``/start`` handler stores inline credentials
            here and replaces them with a ``credential_guid`` so secrets
            never travel over Temporal.  Passed directly to avoid ContextVar
            propagation issues with uvicorn ASGI request handlers.
        storage: Optional obstore store for file uploads.
        title: OpenAPI title.
        description: OpenAPI description.
        version: API version string.
        frontend_assets_path: Path to the directory containing frontend static assets.
            Serves ``index.html`` at ``GET /`` and mounts remaining assets as static
            files. Defaults to ``"app/generated/frontend/static"``.

    Returns:
        Configured FastAPI application.
    """
    global _workflow_config, _state_store, _secret_store, _storage

    _state_store = state_store
    _secret_store = secret_store
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

    def _create_context(credentials: list[HandlerCredential]) -> HandlerContext:
        return HandlerContext(
            app_name=app_name,
            request_id=uuid4(),
            started_at=datetime.now(UTC),
            _credentials=credentials,
            _secret_store=_secret_store,
            _state_store=_state_store,
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/auth")
    async def test_auth(request: Request) -> JSONResponse:
        body = _normalize_credentials(await request.json())
        auth_input = AuthInput.model_validate(body)
        credentials = [
            HandlerCredential(key=c.key, value=c.value) for c in auth_input.credentials
        ]
        context = _create_context(credentials)
        handler._context = context

        try:
            logger.info(
                "Auth test started: app=%s request=%s", app_name, context.request_id_str
            )
            result = await handler.test_auth(auth_input)
            logger.info(
                "Auth test completed: app=%s request=%s status=%s",
                app_name,
                context.request_id_str,
                result.status.value,
            )
            return JSONResponse(
                content=_wrap_response(
                    result.model_dump(),
                    message=result.message or f"Authentication {result.status.value}",
                    success=result.status == AuthStatus.SUCCESS,
                )
            )
        except HandlerError as e:
            logger.error(
                "Auth test failed for app %s (request %s): %s",
                app_name,
                context.request_id_str,
                e,
                exc_info=True,
            )
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except Exception as e:
            logger.error(
                "Auth test failed unexpectedly for app %s (request %s): %s",
                app_name,
                context.request_id_str,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Internal server error"
            ) from None
        finally:
            handler._context = None

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/check")
    async def preflight_check(request: Request) -> JSONResponse:
        body = _normalize_credentials(await request.json())
        preflight_input = PreflightInput.model_validate(body)
        credentials = [
            HandlerCredential(key=c.key, value=c.value)
            for c in preflight_input.credentials
        ]
        context = _create_context(credentials)
        handler._context = context

        try:
            logger.info(
                "Preflight check started: app=%s request=%s",
                app_name,
                context.request_id_str,
            )
            result = await handler.preflight_check(preflight_input)
            logger.info(
                "Preflight check completed: app=%s request=%s status=%s checks=%d",
                app_name,
                context.request_id_str,
                result.status.value,
                len(result.checks),
            )
            # Build v2-compatible response: each check becomes a top-level
            # key in data so the frontend can iterate check names directly.
            # v2 format: {"authenticationCheck": {"success": true, "message": "..."}, ...}
            v2_data: dict[str, Any] = {}
            for check in result.checks:
                # Convert check name to camelCase key (e.g. "AuthCheck" -> "authCheck")
                key = check.name[0].lower() + check.name[1:]
                v2_data[key] = {
                    "success": check.passed,
                    "message": check.message or "",
                }
            return JSONResponse(
                content=_wrap_response(
                    v2_data,
                    message=result.message or f"Preflight check {result.status.value}",
                    success=result.status == PreflightStatus.READY,
                )
            )
        except HandlerError as e:
            logger.error(
                "Preflight check failed for app %s (request %s): %s",
                app_name,
                context.request_id_str,
                e,
                exc_info=True,
            )
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except Exception as e:
            logger.error(
                "Preflight check failed unexpectedly for app %s (request %s): %s",
                app_name,
                context.request_id_str,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Internal server error"
            ) from None
        finally:
            handler._context = None

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/metadata")
    async def fetch_metadata(request: Request) -> JSONResponse:
        body = _normalize_credentials(await request.json())
        metadata_input = MetadataInput.model_validate(body)
        credentials = [
            HandlerCredential(key=c.key, value=c.value)
            for c in metadata_input.credentials
        ]
        context = _create_context(credentials)
        handler._context = context

        try:
            logger.info(
                "Metadata fetch started: app=%s request=%s",
                app_name,
                context.request_id_str,
            )
            result = await handler.fetch_metadata(metadata_input)

            # Both SqlMetadataOutput and ApiMetadataOutput expose
            # .objects — model_dump() produces the correct shape for
            # the corresponding frontend widget (sqltree / apitree).
            data = [obj.model_dump() for obj in result.objects]
            count = len(result.objects)
            logger.info(
                "Metadata fetch completed: app=%s request=%s type=%s objects=%d",
                app_name,
                context.request_id_str,
                type(result).__name__,
                count,
            )
            return JSONResponse(
                content=_wrap_response(data, message=f"Fetched {count} objects")
            )
        except HandlerError as e:
            logger.error(
                "Metadata fetch failed for app %s (request %s): %s",
                app_name,
                context.request_id_str,
                e,
                exc_info=True,
            )
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except Exception as e:
            logger.error(
                "Metadata fetch failed unexpectedly for app %s (request %s): %s",
                app_name,
                context.request_id_str,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Internal server error"
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
        workflow_type: str | None = body.pop("workflow_type", None)
        workflow_id = explicit_workflow_id or "(unknown)"

        try:
            client = await _get_temporal_client()

            # Resolve entry point and workflow name from workflow_type.
            # Deferred to avoid a circular import at module load time.
            from application_sdk.app.registry import AppRegistry  # noqa: PLC0415

            app_meta = AppRegistry.get_instance().get(app_cls._app_name)  # type: ignore[attr-defined]
            entry_points = app_meta.entry_points

            if workflow_type:
                if workflow_type not in entry_points:
                    logger.warning(
                        "Unknown workflow_type '%s' for app %s; available: %s",
                        workflow_type,
                        app_name,
                        sorted(entry_points.keys()),
                    )
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid workflow_type.",
                    )
                ep = entry_points[workflow_type]
            elif len(entry_points) == 1:
                ep = next(iter(entry_points.values()))
            elif len(entry_points) > 1:
                logger.warning(
                    "workflow_type required but not provided for multi-entry-point app %s; available: %s",
                    app_name,
                    sorted(entry_points.keys()),
                )
                raise HTTPException(
                    status_code=400,
                    detail="workflow_type is required for this app.",
                )
            else:
                # Fallback: no entry_points (shouldn't happen for a registered app)
                raise HTTPException(
                    status_code=500, detail="App has no registered entry points."
                )

            input_type = ep.input_type
            workflow_name = (
                app_cls._app_name  # type: ignore[attr-defined]
                if ep.implicit
                else f"{app_cls._app_name}:{ep.name}"  # type: ignore[attr-defined]
            )

            if input_type is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"App class {app_cls.__name__} entry point has no input type.",
                )

            # Save inline credentials to the secret store and replace with a
            # credential_guid so raw secrets never travel over Temporal.
            # Uses the closure-captured _secret_store (passed directly to
            # create_app_handler_service) instead of get_infrastructure()
            # because ContextVar does not propagate to uvicorn ASGI request
            # handlers.
            body = _normalize_credentials(body)
            if "credentials" in body and body["credentials"]:
                if _secret_store is not None and hasattr(_secret_store, "set"):
                    credential_guid = str(uuid4())
                    # Convert v3 list [{key, value}] to flat dict so
                    # get_secret() returns the same format as production
                    # (Dapr/Vault). extra.* keys nested under "extra".
                    flat_creds = _pairs_to_flat(body["credentials"])
                    _secret_store.set(credential_guid, json.dumps(flat_creds))
                    body["credential_guid"] = credential_guid
                    del body["credentials"]
                    logger.debug(
                        "Saved inline credentials to secret store: guid=%s",
                        credential_guid,
                    )
                else:
                    logger.warning(
                        "Secret store not writable; inline credentials will be "
                        "passed through on the workflow input."
                    )

            input_data = input_type(**body)

            if explicit_workflow_id:
                workflow_id = explicit_workflow_id
            else:
                config_hash = input_data.config_hash()
                workflow_id = f"{app_name}-{config_hash}-{uuid4().hex[:8]}"

            # Populate framework-managed fields on input_data before Temporal dispatch.
            # These fields are declared on Input (contracts/base.py) but the /start
            # handler constructs input_data before generating them — so they must be
            # injected after the fact.
            #
            # workflow_id: always set by the framework (caller value is popped at
            #   line 607 and used only if explicitly provided).
            # correlation_id: respect caller-supplied value if present (docstring:
            #   "Caller-supplied correlation ID for tracing across systems"), only
            #   generate a UUID when the caller didn't provide one.
            input_data.workflow_id = workflow_id

            correlation_id = input_data.correlation_id or str(uuid4())
            input_data.correlation_id = correlation_id
            input_data._correlation_id = correlation_id

            from application_sdk.observability.correlation import (
                CorrelationContext,
                set_correlation_context,
            )

            set_correlation_context(CorrelationContext(correlation_id=correlation_id))
            corr_ctx = CorrelationContext(correlation_id=correlation_id)

            logger.info(
                "Starting workflow: app=%s workflow_id=%s queue=%s",
                app_name,
                workflow_id,
                _workflow_config.task_queue,
            )

            handle = await client.start_workflow(
                workflow_name,
                args=[input_data],
                id=workflow_id,
                task_queue=_workflow_config.task_queue,
                memo=corr_ctx.to_temporal_memo(),
            )

            logger.info(
                "Workflow started: app=%s workflow_id=%s run_id=%s correlation_id=%s",
                app_name,
                handle.id,
                handle.result_run_id,
                correlation_id,
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
                        "Failed to save workflow config to state store for workflow %s",
                        handle.id,
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
                "Failed to stop workflow %s run %s: %s",
                workflow_id,
                run_id,
                error_msg,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Failed to stop workflow"
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
                    output_type = getattr(
                        _workflow_config.app_class, "_output_type", None
                    )
                    result_data = await _get_workflow_result(
                        client, workflow_id=workflow_id, output_type=output_type
                    )
                    return JSONResponse(
                        content=_wrap_response(
                            {
                                "status": "completed",
                                "workflow_id": workflow_id,
                                "result": result_data,
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
                    output_type = getattr(
                        _workflow_config.app_class, "_output_type", None
                    )
                    result_data = await _get_workflow_result(
                        client, workflow_id=workflow_id, output_type=output_type
                    )
                    return JSONResponse(
                        content=_wrap_response(
                            {
                                "status": "completed",
                                "workflow_id": workflow_id,
                                "result": result_data,
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
                "Failed to get workflow result for %s: %s",
                workflow_id,
                error_msg,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Failed to get workflow result"
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
                "Failed to get workflow status for %s run %s: %s",
                workflow_id,
                run_id,
                error_msg,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Failed to get workflow status"
            ) from None

    # ------------------------------------------------------------------
    # Config (state store)
    # ------------------------------------------------------------------

    def _config_objectstore_key(config_id: str, config_type: str = "workflows") -> str:
        """Build S3 key matching v2 SDK statestore path convention.

        Path: persistent-artifacts/apps/{app_name}/{type}/{id}/config.json
        """
        from application_sdk.constants import APPLICATION_NAME

        return f"persistent-artifacts/apps/{APPLICATION_NAME}/{config_type}/{config_id}/config.json"

    async def _config_load_from_objectstore(
        config_id: str, config_type: str = "workflows"
    ) -> "dict[str, Any] | None":
        """Load workflow config from object store (S3) fallback."""
        if _storage is None:
            return None
        import json as _json
        import os
        import tempfile

        from application_sdk.storage.ops import download_file

        key = _config_objectstore_key(config_id, config_type)
        tmp = tempfile.mktemp(suffix=".json")
        try:
            await download_file(key, tmp, _storage)
            with open(tmp) as f:
                return _json.load(f)
        except Exception:
            return None
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    async def _config_save_to_objectstore(
        config_id: str, body: "dict[str, Any]", config_type: str = "workflows"
    ) -> bool:
        """Save workflow config to object store (S3) fallback."""
        if _storage is None:
            return False
        import json as _json
        import os
        import tempfile

        from application_sdk.storage.ops import upload_file

        key = _config_objectstore_key(config_id, config_type)
        tmp = tempfile.mktemp(suffix=".json")
        try:
            with open(tmp, "w") as f:
                _json.dump(body, f)
            await upload_file(key, tmp, _storage)
            return True
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @app.get("/workflows/v1/config/{config_id}")
    async def get_workflow_config(
        config_id: str, type: str = "workflows"
    ) -> JSONResponse:
        """Fetch workflow config — tries statestore first, falls back to object store."""
        config = None

        # Try statestore
        if _state_store is not None:
            try:
                config = await _state_store.load(f"workflows/{config_id}")
            except Exception:
                logger.warning(
                    "State store load failed for config %s (type=%s), trying object store",
                    config_id,
                    type,
                )

        # Fallback to object store (S3)
        if config is None:
            config = await _config_load_from_objectstore(config_id, config_type=type)

        if config is None and _state_store is None and _storage is None:
            raise HTTPException(
                status_code=503,
                detail="No state store or object store configured",
            )
        if config is None:
            raise HTTPException(
                status_code=404, detail=f"Config not found: {config_id}"
            )
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, Any]", config),
                message="Workflow configuration fetched successfully",
            )
        )

    @app.post("/workflows/v1/config/{config_id}")
    async def update_workflow_config(
        config_id: str, request: Request, type: str = "workflows"
    ) -> JSONResponse:
        """Save workflow config — tries statestore first, falls back to object store.

        Object store fallback is only used for non-credential config types
        to avoid persisting sensitive credential data to S3.
        """
        body = await request.json()
        saved = False

        # Try statestore
        if _state_store is not None:
            try:
                await _state_store.save(f"workflows/{config_id}", body)
                saved = True
            except Exception:
                logger.warning(
                    "State store save failed for config %s (type=%s), trying object store",
                    config_id,
                    type,
                )

        # Fallback to object store (S3) for all config types.
        # Credential configs only contain non-sensitive metadata (host, port,
        # authType, extra) — actual secrets are managed by Heracles/Vault.
        if not saved:
            saved = await _config_save_to_objectstore(config_id, body, config_type=type)

        if not saved:
            raise HTTPException(
                status_code=503,
                detail="No state store or object store configured",
            )
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, Any]", body),
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
                response_obj.model_dump(by_alias=True),
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

            # Inject workflow_id so run() can access it via input.workflow_id
            input_data.workflow_id = workflow_id

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
                "Failed to start event-triggered workflow for event %s: %s",
                event_id,
                e,
                exc_info=True,
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
                            cast("dict[str, Any]", configmap),
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
                cast("dict[str, Any]", {"configmaps": configmap_ids}),
                message="ConfigMaps listed successfully",
            )
        )

    # ------------------------------------------------------------------
    # Manifest endpoint
    # ------------------------------------------------------------------

    @app.get("/workflows/v1/manifest")
    async def get_manifest() -> Response:
        if manifest is not None:
            # Programmatic: Pydantic model → JSON bytes, single hop.
            return Response(
                content=manifest.model_dump_json(), media_type="application/json"
            )
        manifest_path = CONTRACT_GENERATED_DIR / "manifest.json"
        if manifest_path.exists():
            # Disk: contract-generated file — serve raw bytes with deployment
            # name substitution. No parse/reserialize: the file is already
            # valid JSON, validated at build time by the contract tooling.
            raw = manifest_path.read_bytes()
            deployment = (DEPLOYMENT_NAME or "default").encode()
            raw = raw.replace(b"{deployment_name}", deployment)
            return Response(content=raw, media_type="application/json")
        raise HTTPException(status_code=404, detail="No manifest available")

    # ------------------------------------------------------------------
    # Backwards-compatibility alias — TACTICAL, remove once Heracles /
    # Automation Engine is updated to call /workflows/v1/manifest instead.
    # v2 exposed an unversioned /manifest endpoint; v3 moved it to the
    # canonical versioned path above.  Until the orchestrator is patched,
    # this alias keeps existing deployments working.
    # TODO(v3-cleanup): delete this route after Heracles/AE migration (BLDX-804).
    # ------------------------------------------------------------------

    @app.get("/manifest", include_in_schema=False)
    async def get_manifest_legacy() -> Response:
        """Unversioned alias for ``GET /workflows/v1/manifest``.

        .. deprecated::
            This route exists **only** for backwards-compatibility with
            Heracles / Automation Engine, which still calls the v2-era
            unversioned ``/manifest`` path.  It will be removed once those
            orchestrators have been updated to use ``/workflows/v1/manifest``.
            Do **not** add new callers of this endpoint.
        """
        return await get_manifest()

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------

    @app.get("/health")
    @app.get("/server/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/ready")
    @app.get("/server/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Prometheus metrics
    # ------------------------------------------------------------------

    if ENABLE_PROMETHEUS_METRICS:

        @app.get("/metrics")
        async def prometheus_metrics() -> Response:
            """Expose application metrics in Prometheus exposition format."""
            from prometheus_client import REGISTRY, generate_latest

            return Response(
                content=generate_latest(REGISTRY),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

    # ------------------------------------------------------------------
    # UI routes
    # ------------------------------------------------------------------

    @app.get("/")
    async def frontend_home() -> HTMLResponse:
        import os

        frontend_html_path = os.path.join(frontend_assets_path, "index.html")
        if os.path.exists(frontend_html_path):
            with open(frontend_html_path, encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse(
            content="<html><body><h1>UI not available</h1></body></html>",
            status_code=404,
        )

    static_dir = Path(frontend_assets_path)
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir)), name="static")
    else:
        logger.warning(
            "Static UI assets not found at %s, skipping static mount",
            static_dir,
        )

    return app


def run_app_handler_service(
    handler: Handler,
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
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
        port: Bind port (default: 8000).
        log_level: Uvicorn log level (default: "info").
        **kwargs: Additional keyword arguments forwarded to
            ``create_app_handler_service()``.
    """
    import uvicorn

    app = create_app_handler_service(handler, **kwargs)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
