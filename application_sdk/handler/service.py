"""FastAPI service for per-app handler operations.

This module provides the FastAPI application that serves handler
operations for a single app. Each app runs its own handler service.

Routes:
    POST /workflows/v1/auth - Test authentication
    POST /workflows/v1/check - Run preflight checks (optionally only some ``tiers``)
    POST /workflows/v1/warmup - Probe the source's compute once (stateless)
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
from pydantic import ValidationError
from temporalio.client import WorkflowFailureError

from application_sdk._runtime.offload import run_in_thread
from application_sdk.common._generated_tree import (  # noqa: F401 — MANIFEST_STEM, choose_form_configmap, names_entrypoint re-exported (served here before metadata_routes)
    MANIFEST_STEM,
    choose_form_configmap,
    eligible_form_configmaps,
    names_entrypoint,
)
from application_sdk.common.build_identity import (  # noqa: F401 — re-exported (served here before metadata_routes)
    BUILD_IDENTITY_CONFIGMAP_ID,
    build_identity,
)
from application_sdk.common.dispatch import (
    canonical_workflow_type,
    resolve_dispatch_workflow_id,
)
from application_sdk.common.task_queue import (  # noqa: F401 — resolve_manifest_tokens re-exported (served here before metadata_routes)
    resolve_manifest_tokens,
    task_queue_from_env,
)
from application_sdk.constants import CONTRACT_GENERATED_DIR as _CONTRACT_GENERATED_DIR
from application_sdk.constants import DEPLOYMENT_NAME, LOCAL_ENVIRONMENT
from application_sdk.errors import PreconditionError
from application_sdk.handler.base import Handler
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    EventTriggerConfig,
    FileUploadResponse,
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

# Names this module imported (and so offered) before its handler routes moved to
# the api package; kept importable from here, same objects.
from application_sdk.credentials.ingress import lift_agent_json  # noqa: E402,F401
from application_sdk.errors import FailureCategory  # noqa: E402,F401
from application_sdk.errors.base import (  # noqa: E402,F401
    AppError,
    safe_traceback,
    sanitize_cause_repr,
)
from application_sdk.errors.leaves import InternalError  # noqa: E402,F401
from application_sdk.handler._preflight_outcome import (  # noqa: E402
    PreflightSurface,
    emit_preflight_check_outcome,
    emit_preflight_crash_outcome,
)
from application_sdk.handler._warmup import (  # noqa: E402,F401 — re-exported for existing importers
    bounded_warmup_probe,
    rows_outside_tiers,
    warmup_ceiling_seconds,
    warmup_probe_timeout_seconds,
    warmup_unavailable_error,
)
from application_sdk.handler.base import HandlerError  # noqa: E402,F401
from application_sdk.handler.context import bind_handler_context  # noqa: E402,F401

# The handler HTTP surface lives in the api package and is shared with the
# consolidated API host. These private names are the same objects, kept for the
# worker-only routes below (and tests) that use them.
from application_sdk.handler.contracts import (  # noqa: E402,F401; noqa: E402,F401 — re-exported for existing importers
    AuthInput,
    CheckTier,
    HandlerCredential,
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    WarmupInput,
    WarmupObservation,
    WarmupState,
)
from application_sdk.handler.contracts import (  # noqa: E402,F401 — re-exported
    flatten_credentials_to_pairs as _flatten_to_pairs,
)
from application_sdk.handler.contracts import (  # noqa: E402
    normalize_credentials as _normalize_credentials,
)
from application_sdk.handler.contracts import (  # noqa: E402,F401; noqa: E402,F401 — re-exported for existing importers
    unverifiable_preflight_result,
)
from application_sdk.handler.request_contract import ModelT  # noqa: E402,F401
from application_sdk.handler.request_contract import (  # noqa: E402,F401 — re-exported
    RequestContractError as _RequestContractError,
)
from application_sdk.handler.request_contract import (  # noqa: E402,F401 — re-exported
    validate_request as _validate_request,
)
from application_sdk.handler.routes import (  # noqa: E402,F401 — re-exported for existing importers
    _CATEGORY_TO_HTTP,
    _app_error_to_http_status,
    _normalize_preflight_request,
    _preflight_failure_response,
    _preflight_response,
    _preflight_runtime_summary,
    _summarize_check,
    _validated_entrypoint,
    _wrap_response,
    has_warmup,
    include_app_routers_on,
    register_handler_routes,
)


class _GatePreflightObserver:
    """Reports ``/check`` verdicts and crashes through the preflight gate's emitters.

    The shared route takes an observer so the API host (no gate) can log instead;
    the worker keeps the setup funnel's outcome rows exactly as before.
    """

    def __init__(self, app_name: str) -> None:
        self._app_name = app_name

    def outcome(self, result: Any, *, entrypoint: str, request_id: str) -> None:
        emit_preflight_check_outcome(
            logger,
            self._app_name,
            result,
            surface=PreflightSurface.HTTP,
            entrypoint=entrypoint,
            request_id=request_id,
        )

    def crash(self, exc: BaseException, *, entrypoint: str, request_id: str) -> None:
        emit_preflight_crash_outcome(
            logger,
            self._app_name,
            exc,
            surface=PreflightSurface.HTTP,
            entrypoint=entrypoint,
            request_id=request_id,
        )


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


if TYPE_CHECKING:
    from obstore.store import ObjectStore
    from temporalio.client import Client
    from temporalio.converter import DataConverter

    from application_sdk.app.base import App
    from application_sdk.infrastructure.secrets import SecretStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

    Looks the type up in the app's ``workflow_types`` index — the same index the
    worker registers from — and returns the entry point's declared
    ``output_type`` so the caller can pass it to
    ``get_workflow_handle(result_type=…)`` for typed deserialisation.  Because
    both sides read one index, a ``legacy_workflow_types`` alias resolves here
    as readily as a convention-derived name.

    Returns ``None`` when the type is not one this app registers (e.g. another
    app's workflow, or an external one); the caller falls back to untyped
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

    ep = app_meta.workflow_types.get(workflow_type_name)
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


def _entrypoint_form(entrypoint: str) -> tuple[Path | None, list[Path]]:
    """The generated setup form served for *entrypoint* (see
    :func:`application_sdk.handler.metadata_routes.entrypoint_form`)."""
    return entrypoint_form(CONTRACT_GENERATED_DIR, entrypoint)


def generated_entrypoints() -> list[str]:
    """The entry points the generated tree serves a setup form for.

    Each subdirectory of ``CONTRACT_GENERATED_DIR`` holding an eligible form is
    one entry point (the multi-entry-point layout). With none, a flat form makes
    the app's single entry point, reported as ``""`` (the app-level handler).
    Read from disk so the handler process never imports the app registry.
    """
    if not CONTRACT_GENERATED_DIR.is_dir():
        return []
    nested = sorted(
        d.name
        for d in CONTRACT_GENERATED_DIR.iterdir()
        if d.is_dir() and eligible_form_configmaps(d)
    )
    if nested:
        return nested
    return [""] if eligible_form_configmaps(CONTRACT_GENERATED_DIR) else []


SAGE_V2_WIDGET = "sageV2"
"""The ``ui.widget`` value of the setup form's preflight widget."""


def sagev2_warmup_flag(form: dict[str, Any]) -> bool | None:
    """What a generated setup form declares about the app's warmup.

    Reads the top-level ``config.properties`` (the walk the setup UI makes to
    find its preflight widget) for a property whose ``ui.widget`` is
    ``sageV2``. ``None`` when there is none; otherwise whether any such widget
    sets ``ui.warmup`` to ``true``. A missing ``ui.warmup`` is ``false``.
    """
    config = form.get("config", form)
    properties = config.get("properties") if isinstance(config, dict) else None
    if not isinstance(properties, dict):
        return None
    flags = [
        prop["ui"].get("warmup") is True
        for prop in properties.values()
        if isinstance(prop, dict)
        and isinstance(prop.get("ui"), dict)
        and prop["ui"].get("widget") == SAGE_V2_WIDGET
    ]
    return any(flags) if flags else None


def warn_on_warmup_flag_drift(
    entrypoints: list[str], has_warmup: Callable[[str], bool]
) -> list[str]:
    """Log a WARNING for each entry point whose setup form and handler disagree
    about the warmup. Returns the entry points it warned about.

    The form's SageV2 ``ui.warmup`` flag tells the setup UI to run the warmup
    flow; *has_warmup* says whether the entry point's handler actually has one
    (a module ``warmup`` hook, or an app handler overriding
    ``Handler.warmup``). The contract is the source of the flag, so nothing is
    changed here: a mismatch is only reported. An entry point with no served
    form, or a form with no SageV2 widget, has nothing to compare.
    """
    drifted: list[str] = []
    for entrypoint in entrypoints:
        target, _ = _entrypoint_form(entrypoint)
        if target is None:
            continue
        with open(target, encoding="utf-8") as f:
            form = json.load(f)
        declared = sagev2_warmup_flag(form) if isinstance(form, dict) else None
        if declared is None:
            continue
        actual = has_warmup(entrypoint)
        if declared == actual:
            continue
        drifted.append(entrypoint)
        if actual:
            logger.warning(
                "Entry point %s has a warmup (Handler.warmup is overridden or a "
                "module warmup hook exists) but its setup form %s does not set "
                "warmup on the SageV2 widget: the setup check will never run "
                "the warmup-tier checks, so missing grants on them surface "
                "only on a real run. Set warmup: true on the SageV2 widget in "
                "the app's contract.",
                entrypoint or "<app>",
                target.name,
            )
        else:
            logger.warning(
                "Entry point %s sets warmup on the SageV2 widget of its setup "
                "form %s but has no warmup (Handler.warmup is not overridden "
                "and there is no module warmup hook): the setup UI makes a "
                "/warmup round trip that always answers ready. Remove warmup "
                "from the SageV2 widget, or implement Handler.warmup.",
                entrypoint or "<app>",
                target.name,
            )
    return drifted


# The form-discovery exclusion vocabulary lives in
# `application_sdk.common._generated_tree`, which is the authority: this endpoint is
# what a tenant's /api/service/configmaps/<name> proxies to, and the FND-1667
# route check compares what this serves against the app's committed contract.
# A second copy of "which sibling JSON is a form" would let the server serve one
# file while the check compared against another — and that mismatch would read
# as a contract regression rather than as two divergent exclusion lists.
# `list_configmaps` below still uses its own `manifest`-only exclusion (a
# separate, deliberate decision).


from application_sdk.handler.metadata_routes import (  # noqa: E402
    MAX_FE_INPUTS_BYTES,
    MetadataSource,
    decode_fe_inputs,
    entrypoint_form,
    register_metadata_routes,
)

# Allowlist regex for entrypoint names: letter-start, then letters/digits/hyphens/underscores.
# Identical to the @entrypoint decorator constraint. Used as a path-traversal guard
# in get_manifest() before any filesystem path is constructed.
from application_sdk.handler.routes import _ENTRYPOINT_NAME_RE  # noqa: E402

# Per-entry-point hook signatures resolved by the discovery helpers below.
# ``HandlerFn`` is the per-entry-point handler convention
# (``app.<segment>.handler.{test_auth,preflight_check,fetch_metadata,warmup}``); the
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
_MAX_FE_INPUTS_BYTES = MAX_FE_INPUTS_BYTES


def _decode_fe_inputs(raw: str | None) -> dict[str, Any]:
    """Decode the ``fe_inputs`` query payload (see
    :func:`application_sdk.handler.metadata_routes.decode_fe_inputs`)."""
    return decode_fe_inputs(raw, _MAX_FE_INPUTS_BYTES)


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
            by_workflow_type = (
                app_meta.workflow_types.get(selected_entrypoint)
                if allow_workflow_type
                else None
            )
            if by_workflow_type is not None:
                return app_meta, by_workflow_type
            # conformance: ignore[L009] logs caller-invisible context (the available entrypoints) that the generic HTTPException detail does not carry.
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
            # conformance: ignore[L009] logs caller-invisible context (the available entrypoints) that the generic HTTPException detail does not carry.
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


def _marketplace_entrypoint_contract(entrypoint_name: str) -> Any | None:
    """Resolve a *marketplace* entry point's published input contract from disk.

    Bundle ("uber") apps expose marketplace entry points as generated contract
    directories — ``CONTRACT_GENERATED_DIR/<entrypoint>/manifest.json`` — whose
    DAGs are computed per submission by ``compute_manifest``. Those entry
    points are **not** ``@entrypoint`` workflow registrations (the registry
    holds the DAG-node workflows, e.g. ``<entrypoint>-post``), so registry
    resolution 404s on them and ``/v1/app`` creation breaks even though the
    manifest and configmap routes — which are filesystem-first — serve them
    fine. This gives the input-contract route the same filesystem authority.

    The contract toolkit emits each bundle entry point's ``AppInputContract``
    as ``_input.py``; because the kebab-named generated dir is not a Python
    package, the bundle regen convention relocates it to
    ``app/<entrypoint_snake>/_input.py``. Check the in-place generated path
    first (parity with ``_published_input_contract``), then the relocation
    path.

    Returns ``None`` when the entry point has no generated contract dir or no
    importable ``AppInputContract`` — callers fall back to their existing
    behavior.
    """
    import importlib  # noqa: PLC0415 — cold path: only on /input-contract

    from application_sdk.app.entrypoint import (  # noqa: PLC0415 — circular at module import time
        entrypoint_module_segment,
    )

    # The route already validates the name, but this helper must be safe on
    # its own: the name reaches both a filesystem path and an import path.
    # The regex forbids path separators and dots; the containment check makes
    # the no-traversal property locally provable (py/path-injection).
    if not _ENTRYPOINT_NAME_RE.match(entrypoint_name):
        return None
    generated_root = os.path.realpath(CONTRACT_GENERATED_DIR)
    ep_manifest = os.path.realpath(
        os.path.join(generated_root, entrypoint_name, "manifest.json")
    )
    if not ep_manifest.startswith(generated_root + os.sep):
        return None
    if not os.path.isfile(ep_manifest):
        return None
    ep_module = entrypoint_module_segment(entrypoint_name)
    for module_path in (
        f"app.generated.{ep_module}._input",
        f"app.{ep_module}._input",
    ):
        try:
            module = importlib.import_module(module_path)
        except ImportError:  # conformance: ignore[E008,E014] optional generated module; continue to next candidate
            continue
        contract = getattr(module, "AppInputContract", None)
        if contract is not None and hasattr(contract, "model_json_schema"):
            logger.debug(
                "input-contract: using marketplace AppInputContract from %s "
                "for entrypoint %s (filesystem-resolved bundle entry point)",
                module_path,
                entrypoint_name,
            )
            return contract
    return None


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
                _workflow_config.app_name,
                selected_entrypoint,
                allow_workflow_type=(
                    entrypoint_param is None and legacy_workflow_type is not None
                ),
            )

            input_type = ep.input_type
            workflow_name = canonical_workflow_type(_workflow_config.app_name, ep)

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

            # Populate framework-managed fields on input_data before Temporal dispatch.
            # These fields are declared on Input (contracts/base.py) but the /start
            # handler constructs input_data before generating them — so they must be
            # injected after the fact.
            #
            # workflow_id: resolved and stamped by the shared dispatch helper —
            #   the same body the executor backend uses, so the two dispatch
            #   paths cannot drift. The caller value is popped from the body
            #   before validation and used only if explicitly provided.
            # correlation_id: respect caller-supplied value if present (docstring:
            #   "Caller-supplied correlation ID for tracing across systems"), only
            #   generate a UUID when the caller didn't provide one.
            workflow_id = resolve_dispatch_workflow_id(
                input_data, app_name, explicit_workflow_id=explicit_workflow_id or ""
            )

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
        except ValidationError as e:
            # /start validates the app's own input contract, which the caller
            # does not own — so this is an internal failure, not a malformed
            # request, and it answers a sanitized 500 with the real cause in
            # the log. It cannot reach the 422 handler (that one is registered
            # on _RequestContractError, which only _validate_request raises);
            # catching here is what turns Starlette's plain-text 500 into the
            # boundary's own logged, sanitized one.
            # conformance: ignore[L009] boundary handler logs the real exception (exc_info) then raises a sanitized HTTPException `from None`; the log is the only server-side record of the actual failure.
            logger.error(
                "Workflow input validation failed for app %s: %s",
                app_name,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Failed to start workflow"
            ) from None
        except TypeError as e:
            # conformance: ignore[L009] boundary handler logs the real exception (exc_info) then raises a sanitized HTTPException `from None`; the log is the only server-side record of the actual failure.
            logger.error(
                "Invalid workflow input for app %s: %s", app_name, e, exc_info=True
            )
            raise HTTPException(status_code=400, detail="Invalid input") from None
        except Exception as e:
            # conformance: ignore[L009] boundary handler logs the real exception (exc_info) then raises a sanitized HTTPException `from None`; the log is the only server-side record of the actual failure.
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
            # conformance: ignore[L009] boundary handler logs the real exception (exc_info) then raises a sanitized HTTPException `from None`; the log is the only server-side record of the actual failure.
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
            # conformance: ignore[L009] boundary handler logs the real exception (exc_info) then raises a sanitized HTTPException `from None`; the log is the only server-side record of the actual failure.
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
            # conformance: ignore[L009] boundary handler logs the real exception (exc_info) then raises a sanitized HTTPException `from None`; the log is the only server-side record of the actual failure.
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
                "use a specific config type instead. "
                "Will be removed in v4.0.",
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

            file_size = await run_in_thread(_drain_to_tmp)
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
            # conformance: ignore[L009] the log is the only server-side record; the raised HTTPException deliberately withholds the parse internals.
            logger.warning("Failed to parse JSON request body", exc_info=True)
            raise HTTPException(status_code=400, detail="Invalid JSON body") from None

        try:
            client = await _get_temporal_client()

            from application_sdk.app.entrypoint import (  # noqa: PLC0415 — _resolve_default_entrypoint is private to app.entrypoint
                _resolve_default_entrypoint,
            )

            ep = _resolve_default_entrypoint(
                getattr(getattr(app_cls, "_app_metadata", None), "entry_points", {})
            )
            input_type = (
                ep.input_type
                if ep is not None
                else getattr(app_cls, "_input_type", None)
            )
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

            # Correlation memo (CNCT-104): event-triggered starts previously
            # passed no memo, so the LogInterceptor fell back to a value the
            # run page never queries and the run's logs were unfindable.
            # Respect an event-supplied correlation_id; otherwise mint one —
            # the same contract as the /workflows/v1/start path above — and
            # return it to the caller so the run's logs are addressable.
            correlation_id = getattr(input_data, "correlation_id", "") or str(uuid4())
            input_data.correlation_id = correlation_id
            input_data._correlation_id = correlation_id

            from application_sdk.observability.correlation import (  # noqa: PLC0415 — circular: handler.service is the FastAPI entry point — execution/server modules load app.base which loads handler
                CorrelationContext,
                set_correlation_context,
            )

            # Mirror the /workflows/v1/start path: bind the ContextVar so every
            # handler-side line for this request — including the except branch on
            # a failed dispatch — carries the correlation_id we just returned to
            # the caller, keeping the run addressable even when the start fails.
            set_correlation_context(CorrelationContext(correlation_id=correlation_id))

            handle = await client.start_workflow(
                canonical_workflow_type(app_cls._app_name, ep)
                if ep is not None
                else _workflow_config.app_name,
                args=[input_data],
                id=workflow_id,
                task_queue=_workflow_config.task_queue,
                memo=CorrelationContext(
                    correlation_id=correlation_id
                ).to_temporal_memo(),
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
                    "correlation_id": correlation_id,
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
    # ConfigMaps, manifest and input contract (shared with the host)
    # ------------------------------------------------------------------

    def _registry_default_entrypoint() -> str:
        _, ep = _resolve_app_entrypoint(
            _workflow_config.app_name, None, unknown_ep_status=404
        )
        return ep.name

    def _registry_explicit_entrypoints() -> list[str]:
        # Resolved INDEPENDENTLY of the default: a servable multi-entrypoint
        # app must not 404 just because the builder marked no default. A
        # connector that implements run() AND declares explicit @entrypoints
        # gets run() forced as its default, but run() has no generated manifest
        # dir — so the explicit ones, alphabetically, are the fallback.
        from application_sdk.app.registry import (  # noqa: PLC0415
            AppNotFoundError,
            AppRegistry,
        )

        try:
            app_meta = AppRegistry.get_instance().get(_workflow_config.app_name)
        except AppNotFoundError:
            return []
        return [
            ep.name
            for ep in sorted(app_meta.entry_points.values(), key=lambda e: e.name)
            if not ep.implicit
        ]

    def _registry_input_contract(entrypoint: str | None) -> Any:
        try:
            _, ep = _resolve_app_entrypoint(
                _workflow_config.app_name, entrypoint, unknown_ep_status=404
            )
        except HTTPException as exc:
            # Registry miss — the entry point may be a *marketplace* (bundle)
            # entry point that exists only as a generated contract dir on disk.
            # Filesystem-resolve it like the manifest and configmap routes do;
            # re-raise when it isn't one.
            if exc.status_code != 404 or not entrypoint:
                raise
            contract = _marketplace_entrypoint_contract(entrypoint)
            if contract is None:
                raise
            return contract
        # Prefer the generated AppInputContract over the entry point's thin
        # runtime input_type. See _published_input_contract.
        input_type = _published_input_contract(ep)
        if input_type is None:
            raise HTTPException(
                status_code=500, detail="Entry point has no input type."
            )
        return input_type

    # Every lookup reads this module's globals per request, so a process
    # reconfigured after startup (and a test that repoints them) is seen.
    register_metadata_routes(
        app,
        MetadataSource(
            generated_dir=lambda: CONTRACT_GENERATED_DIR,
            app_name=lambda: _workflow_config.app_name,
            fallback_app_name=app_name,
            task_queue=lambda: _workflow_config.task_queue,
            deployment_name=lambda: DEPLOYMENT_NAME,
            default_entrypoint=_registry_default_entrypoint,
            explicit_entrypoints=_registry_explicit_entrypoints,
            input_contract=_registry_input_contract,
            compute_manifest=lambda ep: _discover_compute_manifest(ep),
            programmatic_manifest=manifest,
            log=lambda: logger,
            max_fe_inputs_bytes=lambda: _MAX_FE_INPUTS_BYTES,
        ),
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

    .. deprecated:: 3.37
        Use :func:`server_sdk.build_asgi_app` from ``atlan-application-sdk-server``.
        It installs without the worker dependency tree and registers the handler
        routes only, so check the routes your app relies on. Removed in v4.0.

    Args:
        handler: The Handler instance to serve.
        app_name: App name for logging context.
        app_class: App class for workflow execution (enables /start, /stop, etc.).
        temporal_host: Temporal server address (e.g., "localhost:7233").
        temporal_namespace: Temporal namespace.
        task_queue: Task queue name. Defaults to the deployment's canonical queue
            (``atlan-{ATLAN_APPLICATION_NAME}-{ATLAN_DEPLOYMENT_NAME}``, see
            :func:`application_sdk.common.task_queue.derive_task_queue`), falling
            back to ``"{app_name}-queue"`` when that env is absent.
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
            "Use DaprCredentialVault, now the exclusive credential-resolution "
            "path; stop passing state_store. "
            "Will be removed in v3.2.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    _secret_store = secret_store
    _storage = storage
    _workflow_config = WorkflowClientConfig(
        host=temporal_host,
        namespace=temporal_namespace,
        # Env derivation before the {app_name}-queue default: this value is what
        # the manifest route stamps into the DAG, so a handler constructed
        # directly (without main.py passing the worker's queue through) must still
        # land on the queue a worker started from the same env would poll — not on
        # a fifth, locally-invented shape (FND-195).
        task_queue=task_queue
        or task_queue_from_env()
        # Guarded on app_name: an unnamed handler would otherwise manufacture a
        # bare "-queue", and the manifest route would stamp that into the DAG.
        or (f"{app_name}-queue" if app_name else ""),
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

    from fastapi.telemetry import (  # noqa: PLC0415 — cold path: only at handler service startup
        TelemetryConfig,
    )

    from application_sdk.constants import (  # noqa: PLC0415 — cold path: only at handler service startup
        ENABLE_MCP,
    )

    # FastAPI >=0.142 auto-configures OTLP/HTTP exporters on the global
    # providers when OTEL_EXPORTER_OTLP_ENDPOINT is set. The SDK owns those
    # providers (observability/*_adaptor.py) and exports gRPC to that endpoint,
    # so FastAPI's exporters would race ours for the global TracerProvider and
    # fail against the gRPC collector. Request telemetry stays with
    # FastAPIInstrumentor below; FastAPI's native middleware defers to it.
    fastapi_telemetry: TelemetryConfig = {"auto_configure": False}

    if ENABLE_MCP and app_name:
        from contextlib import (  # noqa: PLC0415 — cold path: lifespan setup, only when MCP enabled
            asynccontextmanager,
        )

        try:
            from application_sdk.server.mcp import (  # noqa: PLC0415 — cold path: only when ENABLE_MCP set
                MCPServer,
            )
        except ModuleNotFoundError as e:
            # Only the mcp extra's own dependency (fastmcp) being absent means
            # "extra not installed". Any other ModuleNotFoundError in the import
            # chain is an unrelated broken import — re-raise it unchanged so the
            # user isn't sent to reinstall the extra when the fault is elsewhere.
            if e.name != "fastmcp" and not (e.name or "").startswith("fastmcp."):
                raise
            raise PreconditionError(
                message=(
                    "ENABLE_MCP is set but the MCP dependencies are not installed. "
                    "Install the SDK with the 'mcp' extra "
                    "(e.g. `uv add 'atlan-application-sdk[mcp]'`) or unset ENABLE_MCP."
                ),
                resource="mcp extra",
                expected_state="installed",
                actual_state="missing",
                suggested_action=(
                    "Install the SDK with the 'mcp' extra "
                    "(e.g. `uv add 'atlan-application-sdk[mcp]'`) or unset ENABLE_MCP."
                ),
                cause=e,
            ) from e

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
            telemetry=fastapi_telemetry,
        )
    else:
        app = FastAPI(
            title=title,
            description=description,
            version=version,
            telemetry=fastapi_telemetry,
        )

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

    # The auth / check / metadata routes, the request contract's 422 and the
    # error boundary are ONE implementation shared with the consolidated API host
    # (application_sdk.handler.routes). The worker reports /check outcomes through
    # the preflight gate's emitters so the setup funnel keeps its rows.
    register_handler_routes(
        app,
        handler,
        app_name=app_name,
        secret_store=_secret_store,
        observer=_GatePreflightObserver(app_name),
        include_app_routers=False,
        warmup_ceiling=getattr(app_class, "preflight_warmup_ceiling_seconds", None),
        warmup_probe_timeout=getattr(
            app_class, "preflight_warmup_probe_timeout_seconds", None
        ),
    )

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

    # The app's own routers go after every SDK route (so a redefinition is
    # refused) and before the static mount (which would shadow them).
    include_app_routers_on(app, handler, app_name)

    static_dir = Path(frontend_assets_path)
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir)), name="static")
    else:
        logger.warning(
            "Static UI assets not found at %s, skipping static mount",
            static_dir,
        )

    # FND-3334: the setup form's SageV2 warmup flag and the handler must agree.
    # Reported once at startup; never allowed to break the service.
    # Entry points come from the generated tree, not the app registry:
    # importing the registry loads execution code, which the handler process
    # must never do (FND-3280).
    try:
        warn_on_warmup_flag_drift(
            generated_entrypoints(), lambda ep: has_warmup(handler, ep)
        )
    except Exception:
        logger.warning(
            "Could not compare setup forms' warmup flags with the handler",
            exc_info=True,
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

    .. deprecated:: 3.37
        Use :func:`server_sdk.build_asgi_app` with your own ``uvicorn.run``. The
        consolidated host owns the run loop, so ``atlan-application-sdk-server``
        ships no blocking entry point. Removed in v4.0.

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
