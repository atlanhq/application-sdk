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

import asyncio
import dataclasses
import inspect
import json
import mimetypes
import os
import re
import shutil
import tempfile
import warnings
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import TYPE_CHECKING, Annotated, Any, cast
from uuid import uuid4

import orjson
import temporalio.service
from fastapi import FastAPI, File, Form, HTTPException
from fastapi import Path as PathParam
from fastapi import Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel as PydanticBaseModel
from temporalio.client import WorkflowFailureError

from application_sdk.constants import CONTRACT_GENERATED_DIR as _CONTRACT_GENERATED_DIR
from application_sdk.constants import DEPLOYMENT_NAME, LOCAL_ENVIRONMENT
from application_sdk.errors import AppError
from application_sdk.errors.categories import FailureCategory
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.context import HandlerContext, bind_handler_context
from application_sdk.handler.contracts import (
    AuthInput,
    EventTriggerConfig,
    FileUploadResponse,
    HandlerCredential,
    MetadataInput,
    PreflightInput,
    SubscriptionConfig,
)
from application_sdk.handler.manifest import AppManifest
from application_sdk.handler.service_errors import (
    InvalidConfigIdError,
    InvalidConfigTypeError,
    TempPathEscapeError,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_CATEGORY_TO_HTTP: dict[FailureCategory, int] = {
    FailureCategory.AUTH: 401,
    FailureCategory.PERMISSION: 403,
    FailureCategory.NOT_FOUND: 404,
    FailureCategory.ALREADY_EXISTS: 409,
    FailureCategory.INVALID_INPUT: 400,
    FailureCategory.PRECONDITION: 412,
    FailureCategory.RATE_LIMITED: 429,
    FailureCategory.TIMEOUT: 504,
    FailureCategory.DEPENDENCY_UNAVAILABLE: 503,
    FailureCategory.RESOURCE_EXHAUSTED: 503,
    FailureCategory.DATA_INTEGRITY: 500,
    FailureCategory.INTERNAL: 500,
    FailureCategory.UNIMPLEMENTED: 501,
    FailureCategory.CANCELLED: 499,  # client-closed-request (nginx convention)
}


def _app_error_to_http_status(exc: AppError) -> int:
    return _CATEGORY_TO_HTTP.get(exc.category, 500)


def _record_proxy_failure(
    reason: str,
    log_message: str,
    *log_args: object,
    exc_info: bool = False,
) -> None:
    """Record a Temporal-core ``/metrics`` proxy failure.

    Increments the ``temporal_core_metrics_proxy_failures_total`` counter
    so failures are observable from VictoriaMetrics (and not just buried
    in the logs), and emits a bounded diagnostic. ``reason`` should be a
    bounded label value — exception class name or ``http_<status>``."""
    from application_sdk.observability import (  # noqa: PLC0415 — cold path: only on proxy failure
        metrics as _metrics_module,
    )

    counter = _metrics_module.create_counter(
        "temporal_core_metrics_proxy_failures",
        description=(
            "Failures fetching the Temporal Rust-core /metrics endpoint via "
            "the in-process FastAPI proxy. Each increment is one scrape "
            "where Temporal-core series were not retrievable."
        ),
        unit="1",
    )
    counter.add(1, {"reason": reason})
    logger.warning(log_message, *log_args, exc_info=exc_info)


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


# v2-compat: remove when Heracles sends credentials in v3 list[{key, value}] format.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap_response(
    data: dict[str, Any] | list[Any],
    *,
    message: str = "",
    success: bool = True,
) -> dict[str, Any]:
    """Wrap response data in the standard envelope: {success, message, data}.

    ``message`` is omitted from the response when empty to match the legacy
    /credentials/query format expected by the frontend filter widgets.
    """
    result: dict[str, Any] = {"success": success, "data": data}
    if message:
        result["message"] = message
    return result


async def _get_workflow_result(
    client: Client,
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


def _resolve_output_type_for_workflow(workflow_type_name: str) -> type | None:
    """Resolve the correct Output type for a workflow from its Temporal type name.

    Temporal workflow type names are ``"app-name"`` for the implicit (single)
    entry point and ``"app-name:entrypoint-name"`` for explicit named entry
    points.  We parse the entry-point suffix, look it up in AppRegistry, and
    return its declared ``output_type`` so the caller can pass it to
    ``get_workflow_handle(result_type=…)`` for typed deserialisation.

    Returns ``None`` when the entry point cannot be resolved (e.g. missing
    registry entry, external workflow); the caller falls back to untyped
    deserialisation via ``_get_workflow_result``'s own fallback path.
    """
    if _workflow_config.app_class is None:
        return None
    app_cls_name = _workflow_config.app_name
    if not app_cls_name:
        return None

    from application_sdk.app.registry import (  # noqa: PLC0415
        AppNotFoundError,
        AppRegistry,
    )

    try:
        app_meta = AppRegistry.get_instance().get(app_cls_name)
    except AppNotFoundError:
        logger.warning(
            "App %r not found in registry; returning None", app_cls_name, exc_info=True
        )
        return None

    if ":" in workflow_type_name:
        prefix, ep_name = workflow_type_name.split(":", 1)
        if prefix != app_cls_name:
            return None  # workflow belongs to a different app
        ep = app_meta.entry_points.get(ep_name)
    else:
        if workflow_type_name != app_cls_name:
            return None  # workflow belongs to a different app
        # Implicit single-entrypoint (backward-compat run() path)
        ep = next((e for e in app_meta.entry_points.values() if e.implicit), None)
        if ep is None and len(app_meta.entry_points) == 1:
            ep = next(iter(app_meta.entry_points.values()))
            logger.debug(
                "Resolved output_type via single-entrypoint fallback for workflow_type=%s ep=%s",
                workflow_type_name,
                ep.name,
            )

    return ep.output_type if ep else None


# ---------------------------------------------------------------------------
# Workflow client config and singleton
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class WorkflowClientConfig:
    """Configuration for the Temporal client used by the /start endpoint."""

    host: str = ""
    namespace: str = "default"
    task_queue: str = ""
    app_name: str = ""
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

    # Temporal Rust-core metrics
    enable_temporal_core_metrics: bool = True
    prometheus_bind_address: str = ""

    # Workflow execution timeout
    workflow_max_timeout_hours: int | None = None

    def is_configured(self) -> bool:
        return bool(self.host and self.app_class and self.app_name)


_temporal_client: Client | None = None
_workflow_config: WorkflowClientConfig = WorkflowClientConfig()
_handler_auth_manager: Any | None = None
_secret_store: SecretStore | None = None
_storage: ObjectStore | None = None

# Directory where generated contract JSON files are stored
CONTRACT_GENERATED_DIR = Path(_CONTRACT_GENERATED_DIR)

# Allowlist regex for entrypoint names: letter-start, then letters/digits/hyphens/underscores.
# Identical to the @entrypoint decorator constraint. Used as a path-traversal guard
# in get_manifest() before any filesystem path is constructed.
_ENTRYPOINT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


# Per-entry-point hook signatures resolved by the discovery helpers below.
# ``HandlerFn`` is the per-entry-point handler convention
# (``app.<segment>.handler.{test_auth,preflight_check,fetch_metadata}``); the
# input/output stay ``Any`` because the concrete contract pair is selected by
# ``fn_name`` at call time. ``_ComputeManifestFn`` is the dynamic-manifest hook
# (``app.<segment>.core.compute_manifest``) — it must be ``async def`` (the hook
# is prescriptively async-only; the call site simply ``await``s it). A sync
# ``def`` is not discovered and silently falls through to the static manifest.
# TODO(BLDX-1354): the connector-author hook surface is typed only as
# ``dict[str, Any]``; once the manifest / fe_inputs shapes stabilise, replace
# these with Pydantic models (e.g. ``Manifest`` / ``FeInputs``) so hook authors
# get validation + editor support instead of bare dicts.
HandlerFn = Callable[[Any, HandlerContext], Awaitable[Any]]
_ComputeManifestFn = Callable[
    [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]
]


def _import_optional_app_module(dotted: str) -> ModuleType | None:
    """Import an optional consumer-owned module by dotted path.

    Returns ``None`` only when the module *itself* (or one of its parent
    packages) is absent — the expected "this app ships no such hook" case.
    Re-raises when the module exists but fails to import: a transitive
    ``ModuleNotFoundError`` (a missing dependency named somewhere other than the
    target's own dotted path) or any other ``ImportError`` is a real bug in the
    connector's code and must surface, not be silently swallowed into a
    fall-through.
    """
    import importlib  # noqa: PLC0415 — cold path: per-entrypoint discovery only

    try:
        return importlib.import_module(dotted)
    except ModuleNotFoundError as exc:  # conformance: ignore[E008] re-raises if missing dep is not the target module itself
        missing = exc.name or ""
        # Swallow only if what's missing is the target or one of its parents
        # (e.g. ``app``, ``app.<segment>``, ``app.<segment>.core``).
        if missing == dotted or dotted.startswith(f"{missing}."):
            return None
        raise


def _discover_compute_manifest(entrypoint: str) -> _ComputeManifestFn | None:
    """Look for a per-entrypoint ``compute_manifest`` hook.

    Convention: ``app.<segment>.core.compute_manifest`` where ``segment`` is
    :func:`~application_sdk.app.entrypoint.entrypoint_module_segment` of the
    entry-point name. Multi-entrypoint apps that need *dynamic* per-submission
    manifest substitution (placeholder fill-in, SQL generation, full DAG
    rewrite) drop a ``core.py`` next to their package's hand-written code with::

        async def compute_manifest(manifest: dict, fe_inputs: dict) -> dict: ...

    The hook must be ``async def`` — a sync ``def`` is not discovered and the
    route serves the static manifest unchanged. The static manifest (already
    token-substituted) is passed in; the coroutine's return value becomes the
    response body. Apps that don't define this module get the static manifest
    unchanged.

    Returns the callable or ``None`` if the module / attribute is absent or
    not a coroutine function. Best-effort discovery — never raises on import
    failure.
    """
    # Lazy: app.entrypoint pulls in app.base, which imports this module
    # (handler.service is the FastAPI entry point); deferring to this cold
    # path avoids the import cycle.
    from application_sdk.app.entrypoint import (  # noqa: PLC0415
        entrypoint_module_segment,
    )

    segment = entrypoint_module_segment(entrypoint)
    module = _import_optional_app_module(f"app.{segment}.core")
    if module is None:
        return None
    fn = getattr(module, "compute_manifest", None)
    # The hook is prescriptively async-only: the call site ``await``s it
    # directly, so require a coroutine function. A sync ``def`` (or any
    # non-callable) is not discovered and silently falls through to the
    # static manifest — mirroring ``_discover_handler_fn``.
    if fn is not None and not inspect.iscoroutinefunction(fn):
        logger.debug(
            "app.%s.core.compute_manifest found but not async; falling through to static manifest",
            segment,
        )
    return cast(
        "_ComputeManifestFn | None", fn if inspect.iscoroutinefunction(fn) else None
    )


def _validated_entrypoint(name: str) -> str:
    """Return the per-entrypoint name to dispatch to, or ``""`` when none is sent.

    The orchestrator resolves the exact entry-point name (e.g.
    ``asset-export-advanced``) from the Global Marketplace app catalog and
    sends it in the ``entrypoint`` field, so resolution is a direct,
    deterministic reference — there is no parsing, filesystem glob, or
    suffix-matching of the legacy ``connector`` string. The name becomes part
    of a module path (``app.<segment>.handler`` / ``.core``), so we validate
    its format and otherwise use it verbatim.

    - **Empty / absent** → ``""``: single-entrypoint apps send no ``entrypoint``
      and fall through to the app-level ``Handler`` instance, 1:1 with today's
      behavior.
    - **Non-empty but malformed** → ``HTTPException(400)``: a bad name is a
      client error, not a silent fall-back to the default entrypoint. This
      keeps the auth/check/metadata routes consistent with
      ``/workflows/v1/manifest`` and the input-contract route, which already
      reject malformed names with 400.
    """
    if not name:
        return ""
    if not _ENTRYPOINT_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid entrypoint name")
    return name


def _discover_handler_fn(entrypoint: str, fn_name: str) -> HandlerFn | None:
    """Look for a per-entrypoint handler function.

    Convention: ``app.<segment>.handler.<fn_name>`` where ``segment`` is
    :func:`~application_sdk.app.entrypoint.entrypoint_module_segment` of the
    entry-point name and ``fn_name`` is one of ``"test_auth"``,
    ``"preflight_check"``, ``"fetch_metadata"``. Multi-entrypoint apps that
    need *per-entrypoint* lifecycle hooks drop a ``handler.py`` next to their
    package's hand-written code with::

        async def test_auth(input: AuthInput, ctx: HandlerContext) -> AuthOutput: ...
        async def preflight_check(input: PreflightInput, ctx: HandlerContext) -> PreflightOutput: ...
        async def fetch_metadata(input: MetadataInput, ctx: HandlerContext) -> MetadataOutput: ...

    The dispatch is best-effort: if the per-entrypoint module / attribute
    is absent, the route falls through to the app-level ``Handler`` instance
    (``DefaultHandler`` if no custom handler is configured), preserving
    today's single-entrypoint behavior 1:1.

    Precedence & silent fall-through — important when reasoning about which
    code actually runs (mirrored in ``docs/concepts/handlers.md``):

    - When a per-entrypoint ``<fn_name>`` exists, it **pre-empts** the
      app-level ``Handler.<fn_name>`` for that entry point. Defining both
      silently runs the module one; the class method never executes.
    - Resolution is per-op: a module that defines only ``fetch_metadata``
      leaves ``test_auth`` / ``preflight_check`` falling back to the
      app-level ``Handler`` — one entry point can be split across two files.
    - A wrong name or a non-``async`` ``def`` does not match (see the
      ``iscoroutinefunction`` check below) and **silently** falls through
      rather than erroring — so a typo'd hook quietly does nothing.

    Returns the callable or ``None`` if absent.
    """
    # Lazy: app.entrypoint pulls in app.base, which imports this module
    # (handler.service is the FastAPI entry point); deferring to this cold
    # path avoids the import cycle.
    from application_sdk.app.entrypoint import (  # noqa: PLC0415
        entrypoint_module_segment,
    )

    segment = entrypoint_module_segment(entrypoint)
    module = _import_optional_app_module(f"app.{segment}.handler")
    if module is None:
        return None
    fn = getattr(module, fn_name, None)
    # The dispatch ``await``s the result, so require a coroutine function — a
    # sync ``def`` falls through to the app-level Handler rather than blowing up
    # with a TypeError at request time.
    if fn is not None and not inspect.iscoroutinefunction(fn):
        logger.debug(
            "app.%s.%s found but not async; falling through to app-level Handler",
            segment,
            fn_name,
        )
    return cast("HandlerFn | None", fn if inspect.iscoroutinefunction(fn) else None)


#: Max size of the decoded ``fe_inputs`` query payload, in UTF-8 bytes.
#
# ``fe_inputs`` rides in a GET query string (``/workflows/v1/manifest?...``),
# so it is bounded by the request-line caps of whatever proxy fronts the app
# (nginx ``large_client_header_buffers`` defaults to 8 KB; ALB allows ~16 KB).
# This app-side cap turns "payload too large" into a clear 413 instead of an
# opaque upstream truncation/414. 8 KB was chosen empirically from the largest
# connector form we ship (asset-export-advanced, ~65 fields): a fully-populated
# form is ~1.6 KB decoded and a heavy multi-select (lists of qualified names /
# GUIDs / tags) is ~5 KB — so 8 KB leaves headroom without rejecting legitimate
# forms. Anything materially larger should move to a POST body (tracked
# follow-up) rather than grow the query string.
_MAX_FE_INPUTS_BYTES = 8192


def _decode_fe_inputs(raw: str | None) -> dict[str, Any]:
    """Decode the ``fe_inputs`` query payload (JSON, sent URL-encoded by
    Heracles for dynamic-manifest apps). Returns ``{}`` when absent; raises
    ``HTTPException(413)`` when it exceeds :data:`_MAX_FE_INPUTS_BYTES` and
    ``HTTPException(400)`` when present but malformed."""
    if not raw:
        return {}
    if len(raw.encode("utf-8")) > _MAX_FE_INPUTS_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"fe_inputs exceeds the {_MAX_FE_INPUTS_BYTES}-byte limit",
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"fe_inputs is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="fe_inputs must be a JSON object")
    return parsed


# Allowed characters for config_id and config_type path components.
# Prevents path traversal (no slashes, dots, or percent-encoding) when these
# values are interpolated into object-store keys.
_CONFIG_KEY_PATTERN = r"^[a-zA-Z0-9_\-]{1,128}$"
_CONFIG_KEY_RE = re.compile(_CONFIG_KEY_PATTERN)

# Strips any character that is not a simple alphanumeric from file extensions
# before they are used as mkstemp suffixes, preventing taint-flow path traversal.
_SAFE_EXT_RE = re.compile(r"[^a-zA-Z0-9]")


def _validated_temp_path(path: str) -> str:
    """Resolve *path* and assert it lies within the system temp directory.

    All temp-file paths in this module are created via :func:`tempfile.mkstemp`.
    This check makes the invariant explicit so SAST tools can confirm no
    user-supplied value can escape the temp directory.
    """
    tmp_root = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(path)
    if real != tmp_root and not real.startswith(tmp_root + os.sep):
        raise TempPathEscapeError()
    return real


async def _get_temporal_client() -> Client:
    """Get or lazily create the singleton Temporal client."""
    global _temporal_client, _handler_auth_manager

    if _temporal_client is not None:
        return _temporal_client

    from application_sdk.execution import (  # noqa: PLC0415 — circular: handler.service is the FastAPI entry point — execution/server modules load app.base which loads handler
        create_temporal_client,
    )

    api_key: str | None = None
    if _workflow_config.auth_enabled:
        from application_sdk.execution import (  # noqa: PLC0415 — circular: handler.service is the FastAPI entry point — execution/server modules load app.base which loads handler
            TemporalAuthConfig,
            TemporalAuthManager,
        )

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
        "Connecting to Temporal for workflow execution host=%s namespace=%s tls=%s auth=%s",
        _workflow_config.host,
        _workflow_config.namespace,
        _workflow_config.tls_enabled,
        _workflow_config.auth_enabled,
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
        enable_prometheus=_workflow_config.enable_temporal_core_metrics,
        prometheus_bind_address=_workflow_config.prometheus_bind_address,
    )
    logger.info("Connected to Temporal")

    if _handler_auth_manager is not None:
        _handler_auth_manager.start_background_refresh(_temporal_client)
        logger.info("Background token refresh started for handler")

    return _temporal_client


# ---------------------------------------------------------------------------
# Entry-point resolution helper (shared by /start and /input-contract)
# ---------------------------------------------------------------------------


def _resolve_app_entrypoint(
    app_name: str,
    selected_entrypoint: str | None,
    *,
    unknown_ep_status: int = 400,
) -> tuple[Any, Any]:
    """Look up the app in the registry and resolve the target entry point.

    Raises ``HTTPException`` for all failure modes so callers can tail-call
    this function without additional try/except.

    Args:
        app_name: Registered app name (from WorkflowClientConfig).
        selected_entrypoint: The ``?entrypoint=`` value, or ``None`` to use
            the default.
        unknown_ep_status: HTTP status to use when an explicitly named
            entrypoint does not exist.  ``400`` for /start (bad request),
            ``404`` for /input-contract (resource not found).

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
    from application_sdk.app.entrypoint import (  # noqa: PLC0415
        _resolve_default_entrypoint,
    )
    from application_sdk.app.registry import (  # noqa: PLC0415
        AppNotFoundError,
        AppRegistry,
    )

    try:
        app_meta = AppRegistry.get_instance().get(app_name)
    except AppNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"App {app_name!r} not registered"
        ) from None

    entry_points = app_meta.entry_points

    if selected_entrypoint:
        if selected_entrypoint not in entry_points:
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


def _published_input_contract(ep: Any) -> Any:
    """Resolve the entry point's *published* input contract.

    Apps generated by the contract-toolkit ship a rich, declarative
    ``AppInputContract`` — every configured field plus ``CredentialRef``-typed
    credential fields — at ``app/generated/{entrypoint}/_input.py`` (or
    ``app/generated/_input.py`` for a single-entry-point app). That is distinct
    from the thin runtime wrapper the ``@entrypoint`` method accepts (which uses
    ``extra="allow"`` just to carry the AE config dict and hydration state), and
    it is the schema Heracles needs to validate caller inputs and discover
    credential fields by their ``CredentialRef``/``AgentCredentialSpec`` ``$ref``.

    Prefer that generated contract by convention; fall back to the entry point's
    runtime ``input_type`` when no generated contract is importable (so apps that
    don't use the contract-toolkit keep working unchanged).
    """
    import importlib  # noqa: PLC0415 — cold path: only on /input-contract

    ep_module = ep.name.replace("-", "_")
    for module_path in (
        f"app.generated.{ep_module}._input",
        "app.generated._input",
    ):
        try:
            module = importlib.import_module(module_path)
        except ImportError:  # conformance: ignore[E008,E014] optional generated module; continue to next candidate
            continue
        contract = getattr(module, "AppInputContract", None)
        if contract is not None and hasattr(contract, "model_json_schema"):
            logger.debug(
                "input-contract: using generated AppInputContract from %s for entrypoint %s",
                module_path,
                ep.name,
            )
            return contract
    logger.debug(
        "input-contract: no generated AppInputContract for entrypoint %s; "
        "falling back to runtime input_type %s",
        ep.name,
        getattr(ep.input_type, "__name__", ep.input_type),
    )
    return ep.input_type


_WORKFLOW_SENSITIVE_FIELDS = {
    "username",
    "password",
    "extra",
    "url",
    "driverProperties",
    "sodaConnection",
}


def _config_objectstore_key(config_id: str, config_type: str = "workflows") -> str:
    """Build S3 key matching v2 SDK statestore path convention.

    Path: persistent-artifacts/apps/{app_name}/{type}/{id}/config.json
    """
    if not _CONFIG_KEY_RE.match(config_id):
        raise InvalidConfigIdError(config_id=config_id)
    if not _CONFIG_KEY_RE.match(config_type):
        raise InvalidConfigTypeError(config_type=config_type)
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: only when computing app paths
        APPLICATION_NAME,
    )

    return f"persistent-artifacts/apps/{APPLICATION_NAME}/{config_type}/{config_id}/config.json"


async def _config_load_from_objectstore(
    config_id: str, config_type: str = "workflows"
) -> dict[str, Any] | None:
    """Load workflow config from object store (S3) fallback."""
    if _storage is None:
        return None

    from application_sdk.storage.ops import download_file  # noqa: PLC0415

    key = _config_objectstore_key(config_id, config_type)
    fd, tmp = tempfile.mkstemp(suffix=".json")
    safe_tmp = _validated_temp_path(tmp)
    os.close(fd)
    try:
        await download_file(key, safe_tmp, _storage)
        return orjson.loads(Path(safe_tmp).read_bytes())
    except Exception:
        logger.warning("Object-store config load failed for key=%s", key, exc_info=True)
        return None
    finally:
        if os.path.exists(safe_tmp):
            os.unlink(safe_tmp)


async def _config_save_to_objectstore(
    config_id: str, body: dict[str, Any], config_type: str = "workflows"
) -> bool:
    """Save workflow config to object store (S3) fallback."""
    if _storage is None:
        return False

    from application_sdk.storage.ops import put_json  # noqa: PLC0415

    key = _config_objectstore_key(config_id, config_type)
    await put_json(key, body, _storage)
    return True


async def _provision_local_vault(guid: str, body: dict[str, Any]) -> JSONResponse:
    """Split credentials into sensitive/non-sensitive and persist locally.

    Sensitive fields are written to ``./local/dapr/secrets/secrets.json`` keyed by guid.
    Non-sensitive fields are written to object storage via the config endpoint.
    """
    if DEPLOYMENT_NAME != LOCAL_ENVIRONMENT:
        raise HTTPException(status_code=403, detail="Dev-only endpoint")

    sensitive: dict[str, Any] = {}
    non_sensitive: dict[str, Any] = {}
    for key, value in body.items():
        if key in _WORKFLOW_SENSITIVE_FIELDS:
            sensitive[key] = value
        else:
            non_sensitive[key] = value

    # Write sensitive fields to the local secrets file.
    # All secrets are stored in a single JSON file keyed by guid
    # (avoids user input in filenames — CodeQL path-traversal).
    secrets_dir = Path(".", "local", "dapr", "secrets")
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secrets_file = secrets_dir / "secrets.json"
    all_secrets: dict[str, Any] = {}
    if secrets_file.exists():
        all_secrets = orjson.loads(secrets_file.read_bytes())
    all_secrets[guid] = sensitive

    # Atomic write: stage to a sibling temp file, then rename. This avoids
    # a partial/truncated secrets.json if the process is killed mid-write.
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(secrets_dir),
            delete=False,
            mode="wb",
            suffix=".json.tmp",
        ) as tmp:
            tmp.write(orjson.dumps(all_secrets))
            tmp_path = tmp.name
        os.replace(tmp_path, str(secrets_file))
        tmp_path = None  # ownership transferred to secrets_file
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:  # conformance: ignore[E002] best-effort temp secrets-file cleanup; leftover file is non-fatal
                pass

    # Write non-sensitive fields to object storage
    non_sensitive["credentialSource"] = non_sensitive.get("credentialSource", "direct")
    await _config_save_to_objectstore(guid, non_sensitive, config_type="credentials")

    logger.info(
        "Provisioned local credentials: guid=%s sensitive_keys=%s non_sensitive_keys=%s",
        guid,
        sorted(sensitive.keys()),
        sorted(non_sensitive.keys()),
    )

    return JSONResponse(
        content=_wrap_response(
            {"credential_guid": guid},
            message="Credentials provisioned successfully",
        )
    )


def _register_workflow_routes(
    app: FastAPI,
    *,
    app_name: str,
    manifest: AppManifest | None,
    event_triggers: list[EventTriggerConfig],
    subscriptions: list[SubscriptionConfig],
) -> None:
    """Register workflow lifecycle, manifest, input-contract, config, file,
    event, and dev-only routes on *app*."""

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
        # ?entrypoint= query param is the canonical selector; fall back to the
        # legacy body field 'workflow_type' for backward compatibility.
        entrypoint_param: str | None = request.query_params.get("entrypoint")
        legacy_workflow_type: str | None = body.pop("workflow_type", None)
        selected_entrypoint: str | None = entrypoint_param or legacy_workflow_type
        workflow_id = explicit_workflow_id or "(unknown)"

        if legacy_workflow_type is not None and entrypoint_param is None:
            warnings.warn(
                f"App {app_name}: 'workflow_type' body field is deprecated. "
                "Use ?entrypoint=<name> query param instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            logger.warning(
                "App %s: 'workflow_type' body field is deprecated; "
                "use ?entrypoint=<name> query param instead.",
                app_name,
            )

        try:
            client = await _get_temporal_client()

            # Migration note (BLDX-1354): On a multi-entry-point app that has
            # no explicit @entrypoint(default=True), omitting ?entrypoint= now
            # dispatches the alphabetically-first entry point by name instead of
            # returning 400.  Any existing caller of /start on such an app that
            # relies on the 400 response must add an explicit ?entrypoint= param
            # or mark a default with @entrypoint(default=True).  Heracles and
            # private callers should be updated before this SDK version is rolled
            # out to apps with multiple entry points.
            _, ep = _resolve_app_entrypoint(
                _workflow_config.app_name, selected_entrypoint
            )

            input_type = ep.input_type
            workflow_name = (
                _workflow_config.app_name
                if ep.implicit
                else f"{_workflow_config.app_name}:{ep.name}"
            )

            if input_type is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"App class {app_cls.__name__} entry point has no input type.",
                )

            body = _normalize_credentials(body)
            if "credentials" in body:
                # Always strip raw credentials — never pass through to
                # Temporal history. Credentials are resolved at runtime
                # via DaprCredentialVault using credential_guid.
                del body["credentials"]
                logger.debug("Stripped inline credentials from /start request body")

            input_data = input_type.model_validate(body)

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

            from application_sdk.observability.correlation import (  # noqa: PLC0415 — circular: handler.service is the FastAPI entry point — execution/server modules load app.base which loads handler
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
                execution_timeout=(
                    timedelta(hours=_workflow_config.workflow_max_timeout_hours)
                    if _workflow_config.workflow_max_timeout_hours
                    else None
                ),
            )

            logger.info(
                "Workflow started: app=%s workflow_id=%s run_id=%s correlation_id=%s",
                app_name,
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
            if (
                isinstance(e, temporalio.service.RPCError)
                and e.status == temporalio.service.RPCStatusCode.NOT_FOUND
            ):
                raise HTTPException(
                    status_code=404, detail=f"Workflow not found: {workflow_id}"
                ) from None
            logger.error(
                "Failed to stop workflow %s run %s: %s",
                workflow_id,
                run_id,
                str(e),
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
                    desc = await handle.describe()
                    output_type = _resolve_output_type_for_workflow(desc.workflow_type)
                    if output_type is None:
                        logger.warning(
                            "No output_type resolved for workflow_id=%s workflow_type=%s; using untyped deserialization",
                            workflow_id,
                            desc.workflow_type,
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
                    output_type = _resolve_output_type_for_workflow(desc.workflow_type)
                    if output_type is None:
                        logger.warning(
                            "No output_type resolved for workflow_id=%s workflow_type=%s; using untyped deserialization",
                            workflow_id,
                            desc.workflow_type,
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
            elif status in ("FAILED", "TERMINATED", "CANCELED", "TIMED_OUT"):
                return JSONResponse(
                    content=_wrap_response(
                        {
                            "status": "failed",
                            "workflow_id": workflow_id,
                            "error": f"Workflow {status.lower()}",
                        },
                        message="Workflow failed",
                        success=False,
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
            if (
                isinstance(e, temporalio.service.RPCError)
                and e.status == temporalio.service.RPCStatusCode.NOT_FOUND
            ):
                raise HTTPException(
                    status_code=404, detail=f"Workflow not found: {workflow_id}"
                ) from None
            logger.error(
                "Failed to get workflow result for %s: %s",
                workflow_id,
                str(e),
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
            if (
                isinstance(e, temporalio.service.RPCError)
                and e.status == temporalio.service.RPCStatusCode.NOT_FOUND
            ):
                raise HTTPException(
                    status_code=404, detail=f"Workflow not found: {workflow_id}"
                ) from None
            logger.error(
                "Failed to get workflow status for %s run %s: %s",
                workflow_id,
                run_id,
                str(e),
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Failed to get workflow status"
            ) from None

    # ------------------------------------------------------------------
    # Config (state store)
    # ------------------------------------------------------------------

    @app.get("/workflows/v1/config/{config_id}")
    async def get_workflow_config(
        config_id: Annotated[str, PathParam(pattern=_CONFIG_KEY_PATTERN)],
        type: Annotated[str, Query(pattern=_CONFIG_KEY_PATTERN)] = "workflows",
    ) -> JSONResponse:
        """Fetch workflow config from object store."""
        config = await _config_load_from_objectstore(config_id, config_type=type)

        if config is None and _storage is None:
            raise HTTPException(
                status_code=503,
                detail="No object store configured",
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
        config_id: Annotated[str, PathParam(pattern=_CONFIG_KEY_PATTERN)],
        request: Request,
        type: Annotated[str, Query(pattern=_CONFIG_KEY_PATTERN)] = "workflows",
    ) -> JSONResponse:
        """Save workflow config to object store."""
        body = await request.json()

        if type == "workflows":
            warnings.warn(
                "Saving config with type='workflows' is deprecated; "
                "use a specific config type instead.",
                DeprecationWarning,
                stacklevel=2,
            )

        saved = await _config_save_to_objectstore(config_id, body, config_type=type)

        if not saved:
            raise HTTPException(
                status_code=503,
                detail="No object store configured",
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

        from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage.ops imports execution-related
            upload_file as _upload_file,
        )

        raw_name = filename or file.filename or "upload"
        # Strip non-alphanumeric chars and cap at 16 chars for object-store key safety.
        extension = _SAFE_EXT_RE.sub("", PurePosixPath(raw_name).suffix.lstrip("."))[
            :16
        ]
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
        safe_tmp_path = _validated_temp_path(tmp_path)
        try:
            os.close(fd)

            def _drain_to_tmp() -> int:
                with open(safe_tmp_path, "wb") as dst:
                    shutil.copyfileobj(file.file, dst)
                return os.path.getsize(safe_tmp_path)

            file_size = await asyncio.to_thread(_drain_to_tmp)
            await _upload_file(key, safe_tmp_path, _storage)
        finally:
            try:
                os.unlink(safe_tmp_path)
            except FileNotFoundError:  # conformance: ignore[E002] temp file already removed; nothing to clean up
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

    @app.get("/dapr/subscribe")
    async def get_dapr_subscriptions() -> JSONResponse:
        from application_sdk.constants import (  # noqa: PLC0415 — cold path: only when emitting events
            EVENT_STORE_NAME,
        )

        result: list[dict[str, Any]] = []

        for sub in subscriptions:
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

        for trigger in event_triggers:
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

        trigger = next((t for t in event_triggers if t.event_id == event_id), None)
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
            # Log details server-side; the HTTPException intentionally hides the
            # parse internals from the client (avoid leaking file/line/offset).
            logger.warning("Failed to parse JSON request body", exc_info=True)
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
                _workflow_config.app_name,
                args=[input_data],
                id=workflow_id,
                task_queue=_workflow_config.task_queue,
                execution_timeout=(
                    timedelta(hours=_workflow_config.workflow_max_timeout_hours)
                    if _workflow_config.workflow_max_timeout_hours
                    else None
                ),
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
    for _sub in subscriptions:
        _sub_route = f"/subscriptions/v1/{_sub.route}"
        app.add_api_route(_sub_route, _sub.handler, methods=["POST"])

    # ------------------------------------------------------------------
    # ConfigMap endpoints
    # ------------------------------------------------------------------

    @app.get("/workflows/v1/configmap/{config_map_id}")
    async def get_configmap(config_map_id: str) -> JSONResponse:
        # 1. Direct match against any generated configmap file stem.
        #    The setup form normally requests the form file by its stem
        #    (e.g. "snowflake-crawler"), which lands here.
        available_configmaps: list[str] = []
        target: "Path | None" = None
        if CONTRACT_GENERATED_DIR.exists():
            for json_file in CONTRACT_GENERATED_DIR.rglob("*.json"):
                available_configmaps.append(json_file.stem)
                if json_file.stem == config_map_id:
                    target = json_file
                    break

        # 2. Default-entrypoint fallback (aligns with PR #1965 semantics
        #    used by /workflows/v1/start and /workflows/v1/input-contract).
        #    The marketplace UI sometimes builds the configmap URL from the
        #    app/marketplace id (e.g. "atlan-snowflake", bare "snowflake")
        #    rather than the entrypoint-form stem. In that case, resolve to
        #    the app's default entrypoint and serve its form configmap.
        #    Multi-entrypoint apps with no marked default fall through to
        #    404 — the caller must use the explicit form stem.
        if target is None:
            try:
                _, ep = _resolve_app_entrypoint(
                    _workflow_config.app_name, None, unknown_ep_status=404
                )
            except HTTPException:
                ep = None
            if ep is not None:
                # Form configmaps for an entrypoint live under
                # CONTRACT_GENERATED_DIR/<ep.name>/ (see EntryPointMetadata
                # docstring: kebab name on the wire and on disk). Pick the
                # form file by excluding the two well-known non-form siblings:
                # `manifest.json` (DAG manifest) and `atlan-connectors-*.json`
                # (credential template). Sorted for determinism.
                entrypoint_dir = CONTRACT_GENERATED_DIR / ep.name
                if entrypoint_dir.is_dir():
                    for json_file in sorted(entrypoint_dir.glob("*.json")):
                        stem = json_file.stem
                        if stem == "manifest" or stem.startswith("atlan-connectors-"):
                            continue
                        target = json_file
                        break

        if target is not None:
            with open(target) as f:
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

        logger.warning(
            "ConfigMap not found: requested=%s available=%s",
            config_map_id,
            sorted(available_configmaps),
        )
        raise HTTPException(
            status_code=404, detail=f"ConfigMap '{config_map_id}' not found"
        )

    @app.get("/workflows/v1/configmaps")
    async def list_configmaps() -> JSONResponse:
        seen: set[str] = set()
        configmap_ids: list[str] = []
        if CONTRACT_GENERATED_DIR.exists():
            for json_file in CONTRACT_GENERATED_DIR.rglob("*.json"):
                stem = json_file.stem
                if stem == "manifest" or stem in seen:
                    continue
                seen.add(stem)
                configmap_ids.append(stem)
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, Any]", {"configmaps": configmap_ids}),
                message="ConfigMaps listed successfully",
            )
        )

    # ------------------------------------------------------------------
    # Manifest endpoint
    # ------------------------------------------------------------------

    async def _serve_entrypoint_manifest(
        entrypoint_name: str,
        fe_inputs: str | None,
        deployment: bytes,
    ) -> Response:
        """Read <entrypoint>/manifest.json, substitute placeholders, run the
        optional ``compute_manifest`` hook, and return the Response.

        Shared by the explicit-``?entrypoint=`` branch and the default-
        entrypoint fallback below — avoids duplicating the placeholder /
        hook logic across both code paths.
        """
        # Build a registry from the filesystem: {entrypoint_name → manifest_path}.
        # The Path objects in the dict come from glob(), not from user input, so
        # the path that reaches read_bytes() is never tainted by the HTTP param.
        # glob("*/manifest.json") only returns direct children of CONTRACT_GENERATED_DIR,
        # so no is_relative_to guard is needed.
        registry: dict[str, Path] = {
            p.parent.name: p for p in CONTRACT_GENERATED_DIR.glob("*/manifest.json")
        }
        ep_manifest = registry.get(entrypoint_name)
        if ep_manifest is None:
            raise HTTPException(
                status_code=404,
                detail=f"No manifest found for entrypoint {entrypoint_name!r}",
            )
        raw = ep_manifest.read_bytes()
        raw = raw.replace(b"{deployment_name}", deployment)
        raw = raw.replace(b"{app_name}", (app_name or "").encode())

        # Dynamic-manifest hook: if the app defines
        # `app.<entrypoint_snake>.core.compute_manifest`, hand the
        # static manifest + decoded `fe_inputs` to it and use the
        # returned dict as the response body. Apps that don't define
        # the hook get the static manifest unchanged (current behavior).
        compute = _discover_compute_manifest(entrypoint_name)
        if compute is not None:
            fe_inputs_decoded = _decode_fe_inputs(fe_inputs)
            manifest_dict = json.loads(raw)
            try:
                # The hook is async-only (discovery rejects sync defs), so
                # await it directly. Authors doing CPU/IO-bound work inside
                # (SQL generation, full DAG rewrite) own offloading it — e.g.
                # `await asyncio.to_thread(...)` — rather than the SDK
                # guessing on their behalf.
                computed = await compute(manifest_dict, fe_inputs_decoded)
            except HTTPException:
                raise
            except Exception:
                # Don't leak the hook's internals (stack/SQL/paths) to the
                # caller — mirror the other handler routes' generic 500.
                logger.error(
                    "compute_manifest hook failed for entrypoint %r",
                    entrypoint_name,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500, detail="Internal server error"
                ) from None
            if not isinstance(computed, dict):
                logger.error(
                    "compute_manifest for entrypoint %r returned %s, expected dict",
                    entrypoint_name,
                    type(computed).__name__,
                )
                raise HTTPException(status_code=500, detail="Internal server error")
            raw = json.dumps(computed).encode()

        return Response(content=raw, media_type="application/json")

    @app.get("/workflows/v1/manifest")
    async def get_manifest(
        entrypoint: str | None = None,
        fe_inputs: str | None = None,
    ) -> Response:
        deployment = (DEPLOYMENT_NAME or "default").encode()

        if entrypoint:
            # Guard 1 (400): reject obviously-malformed names before touching disk.
            if not _ENTRYPOINT_NAME_RE.match(entrypoint):
                raise HTTPException(status_code=400, detail="Invalid entrypoint name")
            return await _serve_entrypoint_manifest(entrypoint, fe_inputs, deployment)

        # No entrypoint param: single-entrypoint path
        if manifest is not None:
            return Response(
                content=manifest.model_dump_json(), media_type="application/json"
            )
        manifest_path = CONTRACT_GENERATED_DIR / "manifest.json"
        if manifest_path.exists():
            # Disk: contract-generated file — serve raw bytes with placeholder
            # substitution. No parse/reserialize: the file is already valid JSON,
            # validated at build time by the contract tooling.
            raw = manifest_path.read_bytes()
            raw = raw.replace(b"{deployment_name}", deployment)
            raw = raw.replace(b"{app_name}", (app_name or "").encode())
            return Response(content=raw, media_type="application/json")

        # Default-entrypoint fallback (aligns with PR #1965 semantics used
        # by /workflows/v1/start and /workflows/v1/input-contract — same
        # treatment we apply to /workflows/v1/configmap above). Multi-
        # entrypoint apps don't have a root manifest.json by design: each
        # entrypoint has its own under <ep.name>/manifest.json. When the
        # caller (e.g. Heracles/AE pre-validation) omits ?entrypoint=, fall
        # back to the app's default entrypoint instead of 404-ing.
        try:
            _, ep = _resolve_app_entrypoint(
                _workflow_config.app_name, None, unknown_ep_status=404
            )
        except HTTPException:
            ep = None
        if ep is not None:
            try:
                return await _serve_entrypoint_manifest(ep.name, fe_inputs, deployment)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                # Default entrypoint exists but has no manifest file on disk —
                # fall through to the canonical 404 below.

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
    async def get_manifest_legacy(
        entrypoint: str | None = None,
        fe_inputs: str | None = None,
    ) -> Response:
        """Unversioned alias for ``GET /workflows/v1/manifest``.

        .. deprecated::
            This route exists **only** for backwards-compatibility with
            Heracles / Automation Engine, which still calls the v2-era
            unversioned ``/manifest`` path.  It will be removed once those
            orchestrators have been updated to use ``/workflows/v1/manifest``.
            Do **not** add new callers of this endpoint.
        """
        return await get_manifest(entrypoint=entrypoint, fe_inputs=fe_inputs)

    # ------------------------------------------------------------------
    # Input-contract endpoint
    # ------------------------------------------------------------------

    @app.get("/workflows/v1/input-contract")
    async def get_input_contract(entrypoint: str | None = None) -> Response:
        """Return the input contract (JSON Schema) for an entry point.

        This is the app's machine-readable input contract: exactly
        ``AppInputContract.model_json_schema()`` for the selected entry point —
        the same Pydantic model that ``/workflows/v1/start`` validates against.
        Heracles consumes it to validate caller inputs and to discover which
        fields are credentials (a field whose schema is a ``$ref`` to the
        ``CredentialRef``/``AgentCredentialSpec`` def).

        Entry-point resolution mirrors ``/workflows/v1/start``: an explicit
        ``?entrypoint=`` selects it; a single-entry-point app resolves
        automatically; a multi-entry-point app requires the param.
        """
        if entrypoint is not None and not _ENTRYPOINT_NAME_RE.match(entrypoint):
            raise HTTPException(status_code=400, detail="Invalid entrypoint name")

        _, ep = _resolve_app_entrypoint(
            _workflow_config.app_name, entrypoint, unknown_ep_status=404
        )

        # Prefer the generated AppInputContract (rich, validatable, credential
        # refs) over the entry point's thin runtime input_type. See
        # _published_input_contract.
        input_type = _published_input_contract(ep)
        if input_type is None:
            raise HTTPException(
                status_code=500, detail="Entry point has no input type."
            )

        return Response(
            content=json.dumps(input_type.model_json_schema()),
            media_type="application/json",
        )

    # ------------------------------------------------------------------
    # Dev-only: local credential provisioning
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/dev/local-vault")
    async def provision_local_vault(request: Request) -> JSONResponse:
        """Provision credentials for local development (auto-generates GUID)."""
        body: dict[str, Any] = await request.json()
        guid = uuid4().hex
        return await _provision_local_vault(guid, body)


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
    secret_store: SecretStore | None = None,
    storage: ObjectStore | None = None,
    event_triggers: list[EventTriggerConfig] | None = None,
    subscriptions: list[SubscriptionConfig] | None = None,
    manifest: AppManifest | None = None,
    title: str = "Handler Service",
    description: str = "Per-app handler service for authentication, preflight, and metadata operations",
    version: str = "1.0.0",
    frontend_assets_path: str = "app/generated/frontend/static",
    enable_temporal_core_metrics: bool = True,
    prometheus_bind_address: str = "",
    workflow_max_timeout_hours: int | None = None,
    # Deprecated: state_store is no longer used. Credential resolution now
    # goes through DaprCredentialVault exclusively. Passing this parameter
    # emits a DeprecationWarning. Will be removed in v3.2.0.
    state_store: Any = None,
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
        secret_store: Optional secret store for credential resolution.
        storage: Optional obstore store for file uploads.
        title: OpenAPI title.
        description: OpenAPI description.
        version: API version string.
        frontend_assets_path: Path to the directory containing frontend static assets.
            Serves ``index.html`` at ``GET /`` and mounts remaining assets as static
            files. Defaults to ``"app/generated/frontend/static"``.
        enable_temporal_core_metrics: When true, ``GET /metrics`` proxies
            Temporal Rust-core metrics from ``prometheus_bind_address``.
        prometheus_bind_address: Loopback bind address for the Temporal
            Rust-core metrics endpoint. Defaults to the SDK constant.
        workflow_max_timeout_hours: Optional ceiling on workflow execution time in
            hours. When set, passed as ``execution_timeout`` to Temporal on every
            ``/workflows/v1/start`` and ``/events/v1/{event_id}`` call. ``None``
            (the default) means no SDK-level ceiling — the Temporal namespace
            default applies. Reads ``ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS`` env var
            when constructed via :class:`AppConfig`; non-positive values are
            treated as ``None`` with a boot-time warning.

    Returns:
        Configured FastAPI application.
    """
    global _workflow_config, _secret_store, _storage

    if state_store is not None:
        warnings.warn(
            "state_store parameter is deprecated and ignored. "
            "Credential resolution now uses DaprCredentialVault exclusively. "
            "Will be removed in v3.2.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    _secret_store = secret_store
    _storage = storage
    _workflow_config = WorkflowClientConfig(
        host=temporal_host,
        namespace=temporal_namespace,
        task_queue=task_queue or f"{app_name}-queue",
        app_name=getattr(app_class, "_app_name", "") if app_class is not None else "",
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
        enable_temporal_core_metrics=enable_temporal_core_metrics,
        prometheus_bind_address=prometheus_bind_address,
        workflow_max_timeout_hours=workflow_max_timeout_hours,
    )

    from application_sdk.constants import (  # noqa: PLC0415 — cold path: only at handler service startup
        ENABLE_MCP,
    )

    if ENABLE_MCP and app_name:
        from contextlib import (  # noqa: PLC0415 — cold path: lifespan setup, only when MCP enabled
            asynccontextmanager,
        )

        from application_sdk.server.mcp import (  # noqa: PLC0415 — cold path: only when ENABLE_MCP set
            MCPServer,
        )

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

    from opentelemetry.instrumentation.fastapi import (  # noqa: PLC0415 — cold path: FastAPI instrumentor wired at app creation
        FastAPIInstrumentor,
    )

    from application_sdk.server.middleware import (  # noqa: PLC0415 — cold path: middleware setup at app creation
        EXCLUDED_LOG_PATHS,
        LogMiddleware,
    )

    # Auto-instrument HTTP server with OTel: emits http.server.duration,
    # http.server.active_requests, etc. with route-templated http.route labels
    # (no raw-path cardinality blowup). Metrics flow through the global
    # MeterProvider configured in observability/metrics_adaptor.py and land in
    # prometheus_client.REGISTRY via the PrometheusMetricReader.
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=",".join(sorted(EXCLUDED_LOG_PATHS)),
    )
    app.add_middleware(LogMiddleware)

    def _create_context(credentials: list[HandlerCredential]) -> HandlerContext:
        return HandlerContext(
            app_name=app_name,
            request_id=uuid4(),
            started_at=datetime.now(UTC),
            _credentials=credentials,
            _secret_store=_secret_store,
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
        with bind_handler_context(context):
            try:
                logger.info(
                    "Auth test started: app=%s request=%s",
                    app_name,
                    context.request_id_str,
                )
                # Per-entrypoint dispatch: multi-entrypoint apps may ship
                # `app.<segment>.handler.test_auth`. The orchestrator sends the
                # exact entry-point name (resolved from the marketplace
                # catalog), so this is a direct lookup — when it maps to a
                # per-entrypoint module, route to it; else (empty/single-
                # entrypoint) fall through to the app-level `Handler` instance.
                entrypoint = _validated_entrypoint(auth_input.entrypoint)
                ep_fn = (
                    _discover_handler_fn(entrypoint, "test_auth")
                    if entrypoint
                    else None
                )
                if ep_fn is not None:
                    result = await ep_fn(auth_input, context)
                else:
                    result = await handler.test_auth(auth_input)
                logger.info(
                    "Auth test completed: app=%s request=%s status=%s",
                    app_name,
                    context.request_id_str,
                    result.status.value,
                )
                return JSONResponse(
                    status_code=result.status.http_status,
                    content=_wrap_response(
                        result.model_dump(),
                        message=result.message
                        or f"Authentication {result.status.value}",
                        success=result.status.is_success,
                    ),
                )
            except HandlerError as e:
                # TODO(signal-over-noise): [P13] Deprecated path — HandlerError is an
                # AppError subclass caught here first so http_status is preserved.
                # Remove once all connector subclasses raise typed AppError leaves.
                # Tracked alongside the Handler abstract-method contract migration.
                # See typed-error-prescription.md §5 (HandlerError row).
                logger.error(
                    "Auth test failed for app %s (request %s): %s",
                    app_name,
                    context.request_id_str,
                    e,
                    exc_info=True,
                )
                raise HTTPException(status_code=e.http_status, detail=str(e)) from None
            except AppError as e:
                # Forward-looking: typed AppError leaves from connectors that raise
                # non-HandlerError typed errors (already migrated).
                logger.error(
                    "Auth test failed for app %s (request %s): %s",
                    app_name,
                    context.request_id_str,
                    e,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=_app_error_to_http_status(e), detail=str(e)
                ) from None
            except HTTPException:
                # Deliberate HTTP responses (e.g. 400 from a malformed
                # entrypoint name) are already client-facing — pass them
                # through rather than masking them as a generic 500.
                raise
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
        with bind_handler_context(context):
            try:
                logger.info(
                    "Preflight check started: app=%s request=%s",
                    app_name,
                    context.request_id_str,
                )
                # Per-entrypoint dispatch (see test_auth above for rationale).
                entrypoint = _validated_entrypoint(preflight_input.entrypoint)
                ep_fn = (
                    _discover_handler_fn(entrypoint, "preflight_check")
                    if entrypoint
                    else None
                )
                if ep_fn is not None:
                    result = await ep_fn(preflight_input, context)
                else:
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
                # v2 format: {"authenticationCheck": {"success": true,
                # "successMessage": "...", "failureMessage": "..."}, ...}.
                #
                # The SageV2 widget at
                # atlan-frontend/src/workflowsv2/components/dynamicForm2/widget/SageV2.vue:271-273
                # renders ``checkResult.success ? successMessage :
                # failureMessage`` with no fallback to ``message``, so omitting
                # those fields leaves the detail panel blank on a failed check
                # (DBBI-665, WARE-1250). ``message`` is retained so any
                # consumer already reading the v3 field keeps working.
                #
                # This finishes the third sub-mismatch from BLDX-901; PR #1228
                # converted ``checks`` → camelCase keys and ``passed`` →
                # ``success`` but left the message-field rename.
                v2_data: dict[str, Any] = {}
                for check in result.checks:
                    # Convert check name to camelCase key (e.g. "AuthCheck" -> "authCheck")
                    key = check.name[0].lower() + check.name[1:]
                    msg = check.message or ""
                    v2_data[key] = {
                        "success": check.passed,
                        "message": msg,
                        "successMessage": msg if check.passed else "",
                        "failureMessage": "" if check.passed else msg,
                    }
                # Envelope ``success`` reports whether preflight executed at
                # all, not whether every check passed — per-check pass/fail
                # belongs in ``data.<check>.success``. The SageV2 widget at
                # SageV2.vue:249 short-circuits on ``!response.success`` and
                # skips the per-check render loop entirely, so collapsing
                # envelope success to ``status == READY`` (the previous
                # behaviour) made every PARTIAL/NOT_READY response surface
                # as "Check failed" with a blank "Hide details" panel
                # (DBBI-665). Tying envelope success to "any check ran"
                # keeps it false when the handler produced no checks (a
                # genuine preflight-system failure) and lets the widget
                # render per-check rows otherwise.
                return JSONResponse(
                    content=_wrap_response(
                        v2_data,
                        message=result.message
                        or f"Preflight check {result.status.value}",
                        success=len(result.checks) > 0,
                    )
                )
            except HandlerError as e:
                # TODO(signal-over-noise): [P13] Deprecated path — HandlerError is an
                # AppError subclass caught here first so http_status is preserved.
                # Remove once all connector subclasses raise typed AppError leaves.
                # Tracked alongside the Handler abstract-method contract migration.
                # See typed-error-prescription.md §5 (HandlerError row).
                logger.error(
                    "Preflight check failed for app %s (request %s): %s",
                    app_name,
                    context.request_id_str,
                    e,
                    exc_info=True,
                )
                raise HTTPException(status_code=e.http_status, detail=str(e)) from None
            except AppError as e:
                # Forward-looking: typed AppError leaves from connectors that raise
                # non-HandlerError typed errors (already migrated).
                logger.error(
                    "Preflight check failed for app %s (request %s): %s",
                    app_name,
                    context.request_id_str,
                    e,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=_app_error_to_http_status(e), detail=str(e)
                ) from None
            except HTTPException:
                # Deliberate HTTP responses (e.g. 400 from a malformed
                # entrypoint name) are already client-facing — pass them
                # through rather than masking them as a generic 500.
                raise
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

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/metadata")
    async def fetch_metadata(request: Request) -> JSONResponse:
        body = _normalize_credentials(await request.json())
        metadata_input = MetadataInput.model_validate(body)
        # The widget routing key (``metadataTemplateKey`` / ``type`` on the
        # wire) now lands in its documented home, ``metadata_template_key``,
        # via the field's validation alias. Mirror it onto ``object_filter``
        # when that's empty so per-entrypoint hooks reading the legacy field
        # (e.g. asset-export-advanced's tags vs connectors vs typenames widgets)
        # keep working. New hooks can read ``metadata_template_key`` directly.
        if not metadata_input.object_filter and metadata_input.metadata_template_key:
            metadata_input = metadata_input.model_copy(
                update={"object_filter": metadata_input.metadata_template_key}
            )
        credentials = [
            HandlerCredential(key=c.key, value=c.value)
            for c in metadata_input.credentials
        ]
        context = _create_context(credentials)
        with bind_handler_context(context):
            try:
                logger.info(
                    "Metadata fetch started: app=%s request=%s",
                    app_name,
                    context.request_id_str,
                )
                # Per-entrypoint dispatch (see test_auth above for rationale).
                entrypoint = _validated_entrypoint(metadata_input.entrypoint)
                ep_fn = (
                    _discover_handler_fn(entrypoint, "fetch_metadata")
                    if entrypoint
                    else None
                )
                if ep_fn is not None:
                    result = await ep_fn(metadata_input, context)
                else:
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
                # message omitted: a non-empty message field caused the
                # frontend filter widgets to render empty dropdowns
                return JSONResponse(content=_wrap_response(data))
            except HandlerError as e:
                # TODO(signal-over-noise): [P13] Deprecated path — HandlerError is an
                # AppError subclass caught here first so http_status is preserved.
                # Remove once all connector subclasses raise typed AppError leaves.
                # Tracked alongside the Handler abstract-method contract migration.
                # See typed-error-prescription.md §5 (HandlerError row).
                logger.error(
                    "Metadata fetch failed for app %s (request %s): %s",
                    app_name,
                    context.request_id_str,
                    e,
                    exc_info=True,
                )
                raise HTTPException(status_code=e.http_status, detail=str(e)) from None
            except AppError as e:
                # Forward-looking: typed AppError leaves from connectors that raise
                # non-HandlerError typed errors (already migrated).
                logger.error(
                    "Metadata fetch failed for app %s (request %s): %s",
                    app_name,
                    context.request_id_str,
                    e,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=_app_error_to_http_status(e), detail=str(e)
                ) from None
            except HTTPException:
                # Deliberate HTTP responses (e.g. 400 from a malformed
                # entrypoint name) are already client-facing — pass them
                # through rather than masking them as a generic 500.
                raise
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

    _register_workflow_routes(
        app,
        app_name=app_name,
        manifest=manifest,
        event_triggers=list(event_triggers or []),
        subscriptions=list(subscriptions or []),
    )

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

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        """Expose application metrics in Prometheus exposition format.

        Merges two sources into a single response:
        1. The OTel ``PrometheusMetricReader`` registry — covers HTTP metrics
           from FastAPIInstrumentor, the worker's ``MetricsInterceptor``, and
           any custom user metrics via ``application_sdk.observability.metrics``.
        2. The Temporal Runtime's loopback Prometheus endpoint — proxies the
           Rust-core metrics when enabled and reachable (gRPC client-call
           latencies on the server, workflow / activity task latencies,
           sticky cache, etc.) so combined pods have a single scrape target.
        """
        from prometheus_client import (  # noqa: PLC0415 — cold path: prometheus only when /metrics is hit
            REGISTRY,
            generate_latest,
        )

        from application_sdk.constants import (  # noqa: PLC0415 — cold path: only when /metrics is hit
            TEMPORAL_CORE_METRICS_PROXY_TIMEOUT_SECONDS,
            TEMPORAL_PROMETHEUS_BIND_ADDRESS,
        )

        temporal_metrics_body: bytes | None = None

        temporal_bind_address = (
            prometheus_bind_address or TEMPORAL_PROMETHEUS_BIND_ADDRESS
        )
        if enable_temporal_core_metrics and temporal_bind_address:
            temporal_metrics_url = f"http://{temporal_bind_address}/metrics"
            import httpx  # noqa: PLC0415 — cold path: only when Temporal Rust-core proxy enabled

            try:
                async with httpx.AsyncClient(
                    timeout=TEMPORAL_CORE_METRICS_PROXY_TIMEOUT_SECONDS
                ) as client:
                    resp = await client.get(temporal_metrics_url)
                    if resp.status_code == 200:
                        temporal_metrics_body = resp.content
                    else:
                        _record_proxy_failure(
                            f"http_{resp.status_code}",
                            "Temporal core metrics proxy returned HTTP %s from %s; serving /metrics without Temporal core series",
                            resp.status_code,
                            temporal_metrics_url,
                        )
            except httpx.RequestError as exc:
                # Handler-only mode disables this proxy; enabled proxy failures are unexpected.
                _record_proxy_failure(
                    type(exc).__name__,
                    "Temporal core metrics proxy request failed for %s; serving /metrics without Temporal core series",
                    temporal_metrics_url,
                    exc_info=True,
                )

        body = generate_latest(REGISTRY)
        if temporal_metrics_body:
            body = body + b"\n" + temporal_metrics_body

        return Response(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ------------------------------------------------------------------
    # UI routes
    # ------------------------------------------------------------------

    @app.get("/")
    async def frontend_home() -> HTMLResponse:
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
    import uvicorn  # noqa: PLC0415 — cold path: uvicorn only when starting standalone server

    app = create_app_handler_service(handler, **kwargs)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
