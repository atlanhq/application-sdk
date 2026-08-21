"""FastAPI assembly for the server handler surface.

``build_asgi_app(handler)`` returns a FastAPI app exposing the connector serving
routes with their request parsing and response envelopes. It can be run
standalone (``uvicorn``) or, in the common API server, mounted / Host-routed as
a sub-application.

Routes:
    POST /workflows/v1/auth                 → handler.test_auth
    POST /workflows/v1/check                → handler.preflight_check
    POST /workflows/v1/metadata             → handler.fetch_metadata
    GET  /workflows/v1/config/{id}          → load workflow config
    POST /workflows/v1/config/{id}          → save workflow config
    GET  /workflows/v1/configmap/{id}       → generated setup-form configmap
    GET  /workflows/v1/configmaps           → list configmap ids
    POST /workflows/v1/start                → start workflow  (only with [workflow] extra)
    GET  /health, /ready                    → liveness / readiness

Deliberately imports NO temporalio / dapr / obstore / boto3 / pyatlan / otel.
``/start`` and its temporalio dependency live behind the ``[workflow]`` extra
(see :mod:`server_sdk.workflow`) and are registered only when it is installed.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import FastAPI, HTTPException
from fastapi import Path as PathParam
from fastapi import Query, Request
from fastapi.responses import JSONResponse
from server_sdk.config.store import (
    CONFIG_KEY_PATTERN,
    ConfigStore,
    config_objectstore_key,
)
from server_sdk.errors.base import AppError, HandlerError
from server_sdk.errors.categories import FailureCategory
from server_sdk.handler.base import Handler
from server_sdk.handler.contracts import (
    AuthInput,
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    normalize_credentials,
)
from server_sdk.manifest import (
    ENTRYPOINT_NAME_RE,
    ComputeManifest,
    register_manifest_routes,
)
from server_sdk.observability.logger_adaptor import get_logger
from server_sdk.workflow import (
    WORKFLOW_EXTRA_AVAILABLE,
    WorkflowStarter,
    register_start_route,
    starter_from_env,
)

logger = get_logger(__name__)

try:  # orjson: fast, stable JSON serialization for the stringified configmap blob.
    import orjson

    def _orjson_str(obj: Any) -> str:
        return orjson.dumps(obj).decode()

except ModuleNotFoundError:  # pragma: no cover - orjson is a declared core dep

    def _orjson_str(obj: Any) -> str:
        return json.dumps(obj, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Error category → HTTP status
# ---------------------------------------------------------------------------

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
    FailureCategory.CANCELLED: 499,
}


def _app_error_to_http_status(exc: AppError) -> int:
    return _CATEGORY_TO_HTTP.get(exc.category, 500)


# ---------------------------------------------------------------------------
# Response envelope + preflight request shaping
# ---------------------------------------------------------------------------


def _normalize_preflight_request(body: dict[str, Any]) -> dict[str, Any]:
    """Mirror ``metadata`` and ``connection_config`` when exactly one is present."""
    normalized = normalize_credentials(body)
    has_metadata = "metadata" in normalized and normalized["metadata"] is not None
    has_connection_config = (
        "connection_config" in normalized
        and normalized["connection_config"] is not None
    )
    if has_metadata and not has_connection_config:
        return {**normalized, "connection_config": normalized["metadata"]}
    if has_connection_config and not has_metadata:
        return {**normalized, "metadata": normalized["connection_config"]}
    return normalized


def _summarize_check(check: PreflightCheck) -> dict[str, Any]:
    dumped = check.model_dump(mode="json", exclude_none=True)
    dumped["message"] = check.resolved_message
    if check.resolved_suggested_action:
        dumped["suggested_action"] = check.resolved_suggested_action
    return dumped


def _preflight_runtime_summary(result: PreflightOutput) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "message": result.message,
        "total_duration_ms": result.total_duration_ms,
        "checks": [_summarize_check(check) for check in result.checks],
    }


def _wrap_response(
    data: dict[str, Any] | list[Any],
    *,
    message: str = "",
    success: bool = True,
) -> dict[str, Any]:
    """Standard envelope ``{success, data, message?}`` — message omitted when empty."""
    result: dict[str, Any] = {"success": success, "data": data}
    if message:
        result["message"] = message
    return result


# ---------------------------------------------------------------------------
# Entrypoint validation
# ---------------------------------------------------------------------------


def _validated_entrypoint(body: dict[str, Any]) -> None:
    """Reject a malformed ``entrypoint`` with 400 before it reaches a handler.

    Without this the surface fails OPEN: an unparseable or traversal-shaped name
    falls through to whatever default the app's handler returns, so ``/auth``
    answers 200 "success" and ``/check`` answers 200 "ready, 0 checks" for a name
    that identifies nothing. application-sdk 400s these, and callers rely on that
    to distinguish "bad request" from "checks passed".
    """
    entrypoint = body.get("entrypoint") or ""
    if entrypoint and not ENTRYPOINT_NAME_RE.match(str(entrypoint)):
        raise HTTPException(status_code=400, detail="Invalid entrypoint name")


# ---------------------------------------------------------------------------
# Configmap discovery
# ---------------------------------------------------------------------------

_CREDENTIAL_TEMPLATE_PREFIXES = ("atlan-connectors-", "csa-connectors-")


def _is_form_configmap(stem: str) -> bool:
    """True when a generated JSON stem is a setup-form configmap."""
    return stem != "manifest" and not stem.startswith(_CREDENTIAL_TEMPLATE_PREFIXES)


def _norm_cm_id(stem: str) -> str:
    s = stem.lower()
    if s.startswith("atlan-") and not s.startswith(("atlan-connectors-", "atlan-csa-")):
        s = s[len("atlan-") :]
    return s


def _scan_generated(gen_dir: Path) -> list[Path]:
    """Snapshot the generated JSON files once, at app-assembly time.

    Deliberately NOT done per request. ``rglob`` is synchronous, and in a
    consolidated host every hosted app shares one event loop with the kubelet
    probes: eight concurrent unauthenticated GETs to the configmap routes were
    measured pushing ``/server/health`` to 1413ms, past the probe's 1s timeout —
    which restarts the pod and takes every hosted app down with it. The contract
    files are baked into the image and cannot change at runtime, so scanning once
    is both cheaper and more correct.
    """
    if not gen_dir.is_dir():
        return []
    return sorted(gen_dir.rglob("*.json"))


def _default_generated_dir() -> Path:
    """Where to read generated contracts from when the app names no directory.

    ``ATLAN_CONTRACT_GENERATED_DIR`` is honoured only when it points at a
    directory that exists. Two failure modes make that check load-bearing in a
    consolidated host, and both were observed live: an EMPTY value makes
    ``Path("")`` the process CWD, so the configmap routes walk the whole
    filesystem tree under it and serve every co-hosted app's setup forms; and a
    NON-EMPTY value is a single process-global path that cannot be right for more
    than one of five apps, so honouring a stale one blanks every app's manifests.
    An unusable override is therefore ignored in favour of the app-local default.
    """
    override = os.environ.get("ATLAN_CONTRACT_GENERATED_DIR", "").strip()
    if override and Path(override).is_dir():
        return Path(override)
    if override:
        logger.warning(
            "Ignoring ATLAN_CONTRACT_GENERATED_DIR=%r: not an existing directory. "
            "Falling back to the app-local default.",
            override,
        )
    return Path("app/generated")


# ---------------------------------------------------------------------------
# App assembly
# ---------------------------------------------------------------------------


def build_asgi_app(
    handler: Handler,
    *,
    title: str | None = None,
    app_name: str = "",
    config_store: ConfigStore | None = None,
    generated_dir: Path | str | None = None,
    workflow_starter: WorkflowStarter | None = None,
    default_entrypoint: str | None = None,
    compute_manifest: dict[str, ComputeManifest] | None = None,
) -> FastAPI:
    """Wire ``handler`` into a FastAPI app. Safe to mount / Host-route.

    ``config_store`` backs the /config endpoints (``None`` → those endpoints
    report 503, "no object store configured"). ``generated_dir``
    is where /configmap reads generated setup-form JSON (defaults to
    ``ATLAN_CONTRACT_GENERATED_DIR`` or ``app/generated``). ``/start`` is
    registered when a ``workflow_starter`` is injected or the ``[workflow]``
    extra is installed; when the extra is present and no starter is passed, the
    default Temporal starter is built from the environment
    (``ATLAN_TEMPORAL_HOST`` — unset → the route answers 503 "not configured").
    ``default_entrypoint`` is dispatched when a ``/start`` request omits
    ``?entrypoint=`` (otherwise such a request is a 400).
    """
    if title is None:
        title = f"Atlan {app_name.title()} Server" if app_name else "Atlan App Server"
    app = FastAPI(title=title, docs_url="/docs")
    # The app's canonical name — also its in-cluster Service name and the leading
    # label of the Host it's addressed by. The common API server reads this to
    # route by Host header without any per-app configuration.
    app.state.app_name = app_name
    gen_dir = (
        Path(generated_dir) if generated_dir is not None else _default_generated_dir()
    )
    generated_files = _scan_generated(gen_dir)

    if config_store is None:
        # Same pattern as the workflow starter: explicit injection wins, else the
        # deployment environment decides (S3_BUCKET set → S3-backed store; unset →
        # None → /config endpoints answer 503 "not configured").
        from server_sdk.config.s3 import (  # noqa: PLC0415 — avoids importing boto3-adjacent module unless needed
            default_config_store,
        )

        config_store = default_config_store()

    # Config keys are scoped per app_name so a multi-app host (common API
    # server) keeps each app's tree separate — and identical to the tree that
    # app's own standalone server/worker reads (ATLAN_APPLICATION_NAME == app
    # name in per-app charts).
    _key_app = app_name or None

    async def _config_load(config_id: str, config_type: str) -> dict[str, Any] | None:
        if config_store is None:
            return None
        return await config_store.load(
            config_objectstore_key(config_id, config_type, app_name=_key_app)
        )

    async def _config_save(
        config_id: str, body: dict[str, Any], config_type: str
    ) -> bool:
        if config_store is None:
            return False
        await config_store.save(
            config_objectstore_key(config_id, config_type, app_name=_key_app), body
        )
        return True

    # -- auth ----------------------------------------------------------------
    @app.post("/workflows/v1/auth")
    async def test_auth(request: Request) -> JSONResponse:
        body = normalize_credentials(await request.json())
        _validated_entrypoint(body)
        auth_input = AuthInput.model_validate(body)
        try:
            logger.info("Auth test started: app=%s", app_name)
            result = await handler.test_auth(auth_input)
            logger.info(
                "Auth test completed: app=%s status=%s", app_name, result.status.value
            )
            return JSONResponse(
                status_code=result.status.http_status,
                content=_wrap_response(
                    result.model_dump(),
                    message=result.message or f"Authentication {result.status.value}",
                    success=result.status.is_success,
                ),
            )
        except HandlerError as e:
            logger.error("Auth test failed for app %s: %s", app_name, e, exc_info=True)
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except AppError as e:
            logger.error("Auth test failed for app %s: %s", app_name, e, exc_info=True)
            raise HTTPException(
                status_code=_app_error_to_http_status(e), detail=str(e)
            ) from None
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                "Auth test failed unexpectedly for app %s: %s",
                app_name,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Internal server error"
            ) from None

    # -- check ---------------------------------------------------------------
    @app.post("/workflows/v1/check")
    async def preflight_check(request: Request) -> JSONResponse:
        body = _normalize_preflight_request(await request.json())
        _validated_entrypoint(body)
        preflight_input = PreflightInput.model_validate(body)
        try:
            logger.info("Preflight check started: app=%s", app_name)
            result = await handler.preflight_check(preflight_input)
            logger.info(
                "Preflight check completed: app=%s status=%s checks=%d",
                app_name,
                result.status.value,
                len(result.checks),
            )
            # v2-compatible response: each check becomes a top-level key in data,
            # keyed by name with only the first char lowercased. successMessage /
            # failureMessage populated per pass/fail so the SageV2 widget renders.
            v2_data: dict[str, Any] = {}
            for check in result.checks:
                key = check.name[0].lower() + check.name[1:]
                msg = check.resolved_message or ""
                v2_data[key] = {
                    "success": check.passed,
                    "message": msg,
                    "successMessage": msg if check.passed else "",
                    "failureMessage": "" if check.passed else msg,
                }
            # Envelope success = "any check ran", NOT "all passed"; the verdict
            # lives in data.<check>.success and preflight.status.
            response = _wrap_response(
                v2_data,
                message=result.message or f"Preflight check {result.status.value}",
                success=len(result.checks) > 0,
            )
            response["preflight"] = _preflight_runtime_summary(result)
            return JSONResponse(content=response)
        except HandlerError as e:
            logger.error(
                "Preflight check failed for app %s: %s", app_name, e, exc_info=True
            )
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except AppError as e:
            logger.error(
                "Preflight check failed for app %s: %s", app_name, e, exc_info=True
            )
            raise HTTPException(
                status_code=_app_error_to_http_status(e), detail=str(e)
            ) from None
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                "Preflight check failed unexpectedly for app %s: %s",
                app_name,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Internal server error"
            ) from None

    # -- metadata ------------------------------------------------------------
    @app.post("/workflows/v1/metadata")
    async def fetch_metadata(request: Request) -> JSONResponse:
        body = normalize_credentials(await request.json())
        _validated_entrypoint(body)
        metadata_input = MetadataInput.model_validate(body)
        # Mirror the widget routing key onto object_filter when it's empty.
        if not metadata_input.object_filter and metadata_input.metadata_template_key:
            metadata_input = metadata_input.model_copy(
                update={"object_filter": metadata_input.metadata_template_key}
            )
        try:
            logger.info("Metadata fetch started: app=%s", app_name)
            result = await handler.fetch_metadata(metadata_input)
            data = [obj.model_dump() for obj in result.objects]
            logger.info(
                "Metadata fetch completed: app=%s type=%s objects=%d",
                app_name,
                type(result).__name__,
                len(result.objects),
            )
            # message deliberately omitted (empty) so FE filter dropdowns render.
            return JSONResponse(content=_wrap_response(data))
        except HandlerError as e:
            logger.error(
                "Metadata fetch failed for app %s: %s", app_name, e, exc_info=True
            )
            raise HTTPException(status_code=e.http_status, detail=str(e)) from None
        except AppError as e:
            logger.error(
                "Metadata fetch failed for app %s: %s", app_name, e, exc_info=True
            )
            raise HTTPException(
                status_code=_app_error_to_http_status(e), detail=str(e)
            ) from None
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                "Metadata fetch failed unexpectedly for app %s: %s",
                app_name,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="Internal server error"
            ) from None

    # -- config --------------------------------------------------------------
    @app.get("/workflows/v1/config/{config_id}")
    async def get_workflow_config(
        config_id: Annotated[str, PathParam(pattern=CONFIG_KEY_PATTERN)],
        type: Annotated[str, Query(pattern=CONFIG_KEY_PATTERN)] = "workflows",
    ) -> JSONResponse:
        config = await _config_load(config_id, config_type=type)
        if config is None and config_store is None:
            raise HTTPException(status_code=503, detail="No object store configured")
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
        config_id: Annotated[str, PathParam(pattern=CONFIG_KEY_PATTERN)],
        request: Request,
        type: Annotated[str, Query(pattern=CONFIG_KEY_PATTERN)] = "workflows",
    ) -> JSONResponse:
        body = await request.json()
        if type == "workflows":
            warnings.warn(
                "Saving config with type='workflows' is deprecated; "
                "use a specific config type instead. Will be removed in v4.0.",
                DeprecationWarning,
                stacklevel=2,
            )
        saved = await _config_save(config_id, body, config_type=type)
        if not saved:
            raise HTTPException(status_code=503, detail="No object store configured")
        return JSONResponse(
            content=_wrap_response(
                cast("dict[str, Any]", body),
                message="Workflow configuration updated successfully",
            )
        )

    # -- configmap -----------------------------------------------------------
    @app.get("/workflows/v1/configmap/{config_map_id}")
    async def get_configmap(config_map_id: str) -> JSONResponse:
        available_configmaps: list[str] = []
        target: Path | None = None
        fuzzy: Path | None = None
        requested_norm = _norm_cm_id(config_map_id)
        if True:
            for json_file in generated_files:
                available_configmaps.append(json_file.stem)
                if json_file.stem == config_map_id:
                    target = json_file
                    break
                if (
                    fuzzy is None
                    and _is_form_configmap(json_file.stem)
                    and _norm_cm_id(json_file.stem) == requested_norm
                ):
                    fuzzy = json_file
        if target is None:
            target = fuzzy

        # Default-entrypoint fallback: each server hosts exactly one app, so when
        # a configmap is requested by app id rather than by form stem we serve the
        # first eligible form configmap in the flat generated dir. Covers the
        # common flat single-entrypoint case.
        if target is None and gen_dir.is_dir():
            for json_file in sorted(gen_dir.glob("*.json")):
                if _is_form_configmap(json_file.stem):
                    target = json_file
                    break

        if target is not None:
            with open(target) as f:
                raw = json.load(f)
            data: dict[str, Any] = {"config": _orjson_str(raw.get("config", raw))}
            default_connector_type = raw.get("defaultConnectorType")
            if default_connector_type is not None:
                data["defaultConnectorType"] = default_connector_type
            configmap = {
                "kind": "ConfigMap",
                "apiVersion": "v1",
                "metadata": {"name": config_map_id},
                "data": data,
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
        if True:
            for json_file in generated_files:
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

    # -- start ---------------------------------------------------------------
    # Registered when a starter is injected (a WorkflowStarter is pure-Python
    # and needs no temporalio) OR the [workflow] extra is installed, in which
    # case the default Temporal starter is built from the environment.
    if workflow_starter is None and WORKFLOW_EXTRA_AVAILABLE:
        workflow_starter = starter_from_env(app_name)
    if workflow_starter is not None or WORKFLOW_EXTRA_AVAILABLE:
        register_start_route(
            app,
            app_name=app_name,
            starter=workflow_starter,
            default_entrypoint=default_entrypoint,
        )
    else:
        logger.info(
            "Workflow routes disabled: no starter injected and temporalio not "
            "installed (install the [workflow] extra to enable /workflows/v1/start)."
        )

    # -- manifest ------------------------------------------------------------
    # Registered for every app that ships generated manifests. Not optional and
    # not app-side: heracles POSTs /manifest during an AE submit and recovers
    # only from a 405, so an app with no such route 404s and the submit aborts
    # (see server_sdk.manifest). Apps without a generated tree are unaffected.
    register_manifest_routes(
        app,
        app_name=app_name,
        generated_dir=gen_dir,
        compute_manifest=compute_manifest,
    )

    # -- health --------------------------------------------------------------
    @app.get("/health")
    @app.get("/server/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    @app.get("/server/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"app": app_name or title, "sdk": "atlan-server-sdk"}

    return app
