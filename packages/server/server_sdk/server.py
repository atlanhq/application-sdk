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
from server_sdk.handler.request_contract import (
    RequestContractError,
    validate_request,
)
from server_sdk.manifest import (
    ENTRYPOINT_NAME_RE,
    ComputeManifest,
    register_manifest_routes,
)
from server_sdk.observability.logger_adaptor import get_logger
from server_sdk.revision import (
    SERVER_SDK_DIST,
    ServerRevision,
    header_safe,
    resolve_app_version,
    server_revision,
)
from server_sdk.workflow import (
    WORKFLOW_EXTRA_AVAILABLE,
    WorkflowStarter,
    register_start_route,
    starter_from_env,
)

logger = get_logger(__name__)

# Response headers carrying the build identity (see server_sdk.revision).
APP_VERSION_HEADER = "X-Atlan-App-Version"
SERVER_REVISION_HEADER = "X-Atlan-Server-Revision"

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
    # The customer's source being down is a 503, not a 500: it was absent from
    # this map and fell through to the 500 default, so every connector that
    # reports SOURCE_UNAVAILABLE was answering as if it had itself broken.
    FailureCategory.SOURCE_UNAVAILABLE: 503,
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
    """Mirror ``metadata`` and ``connection_config`` when exactly one is present.

    The camelCase spelling is folded onto the field name first. ``PreflightInput``
    accepts either via ``AliasChoices``, but the mirror below decides which of
    the two blocks to copy by inspecting the *raw body*, so without the fold a
    caller sending only ``connectionConfig`` + no ``metadata`` would get an
    empty ``metadata`` — and, before the alias existed, an empty
    ``connection_config`` as well.
    """
    normalized = normalize_credentials(body)
    if "connectionConfig" in normalized and "connection_config" not in normalized:
        normalized = {
            k: v for k, v in normalized.items() if k != "connectionConfig"
        } | {"connection_config": normalized["connectionConfig"]}
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
    # cause_repr is the raw exception text behind a typed error. It stays in the
    # server log and the Temporal payload; the HTTP caller gets typed fields.
    dumped = check.model_dump(
        mode="json", exclude_none=True, exclude={"error": {"cause_repr"}}
    )
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
# Build-identity stamping
# ---------------------------------------------------------------------------


class _RevisionHeaderMiddleware:
    """Stamp every HTTP response with the app version and server revision.

    A raw ASGI middleware, not ``BaseHTTPMiddleware``: it injects two entries
    into the ``http.response.start`` message and touches nothing else, so it
    costs no task group, no anyio stream, and no response buffering per request.
    In a consolidated host this runs on every request to every hosted app on one
    shared event loop, which is the wrong place for BaseHTTPMiddleware's
    overhead.

    Working at the message level is also what makes the coverage total: it does
    not matter which route matched or whether one matched at all, so the router
    404, Starlette's method-not-allowed 405 and every ``HTTPException`` handled
    upstream are all stamped without any route-level cooperation.

    The one response this cannot reach is the 500 that
    ``ServerErrorMiddleware`` synthesizes for an unhandled exception — that
    middleware sits *outside* every user middleware by construction. That gap is
    closed by the ``Exception`` handler :func:`build_asgi_app` registers, which
    Starlette installs as ``ServerErrorMiddleware``'s own handler — but only for
    an app that passed ``app_package``, since registering it also replaces
    Starlette's plain-text 500 body with a JSON envelope, and an app that has
    not adopted the stamp has not agreed to that.
    """

    __slots__ = ("app", "_headers")

    def __init__(self, app: Any, headers: list[tuple[bytes, bytes]]) -> None:
        self.app = app
        self._headers = headers

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                # An app that stamps its own value keeps it; we never duplicate.
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in self._headers
                    if name not in present
                )
            await send(message)

        await self.app(scope, receive, _send)


def _revision_headers(
    app_version: str, revision: ServerRevision
) -> list[tuple[bytes, bytes]]:
    """Pre-encoded header pairs — built once per app, not per request.

    **Both** halves go through :func:`~server_sdk.revision.header_safe`. They
    used not to: the revision half was sanitized and ``app_version`` got only
    ``encode("ascii", "replace")``, which is not a sanitizer — CR and LF are
    ASCII and pass straight through. ``app_version`` is a distribution's
    ``Version`` field, i.e. metadata this process did not write, so a value
    containing a CRLF would have been emitted raw into the response start
    message and split into an extra header on the wire. Asymmetric sanitization
    of two values from the same source is a bug even when today's inputs look
    tame.
    """
    return [
        (
            APP_VERSION_HEADER.lower().encode("ascii"),
            header_safe(app_version).encode("ascii", "replace"),
        ),
        (
            SERVER_REVISION_HEADER.lower().encode("ascii"),
            revision.as_header().encode("ascii", "replace"),
        ),
    ]


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
    app_package: str | None = None,
    app_dist: str | None = None,
    app_version: str | None = None,
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

    Build identity
    --------------
    ``app_package`` is the *import* name of this app's server package (e.g.
    ``"redshift_server"``). Given it, every response on every route carries
    ``X-Atlan-App-Version`` and ``X-Atlan-Server-Revision``, and the /manifest
    family repeats both in its JSON body — see :mod:`server_sdk.revision` for
    what the three-part revision means. ``app_dist`` overrides the distribution
    lookup when the import name does not imply it, and ``app_version`` overrides
    the version outright.

    It is deliberately a parameter and not an environment lookup: in the
    consolidated host ``ATLAN_APPLICATION_NAME`` names the *host*, so every
    hosted app would stamp itself ``common-app-server``. Omitting it is
    supported — the headers still appear, reading ``unknown``.

    Passing ``app_package`` is also what opts an app into the stamped 500: the
    ``Exception`` handler that reaches ``ServerErrorMiddleware``'s response is
    registered only then. Registering it unconditionally would have changed the
    500 *body* of every app this function has ever built — Starlette's
    plain-text ``Internal Server Error`` silently becoming a JSON envelope —
    for apps that asked for nothing. An unadopted app keeps Starlette's default.
    """
    if title is None:
        title = f"Atlan {app_name.title()} Server" if app_name else "Atlan App Server"
    app = FastAPI(title=title, docs_url="/docs")
    # The app's canonical name — also its in-cluster Service name and the leading
    # label of the Host it's addressed by. The common API server reads this to
    # route by Host header without any per-app configuration.
    app.state.app_name = app_name

    # Build identity, computed once here rather than per request. Cached across
    # apps by package name, so two sub-apps in one host each pay for their own
    # package exactly once and get two different app_source_digests.
    revision = server_revision(app_package, app_dist)
    version = resolve_app_version(app_package, app_dist, app_version)
    app.state.app_version = version
    app.state.server_revision = revision
    revision_headers = _revision_headers(version, revision)
    app.add_middleware(_RevisionHeaderMiddleware, headers=revision_headers)

    @app.exception_handler(RequestContractError)
    async def _handle_request_contract_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Turn a request-body contract failure into a 422 naming the field.

        Registered unconditionally, unlike the ``Exception`` handler below:
        this only changes responses that are 500s today, so that handler's
        ``app_package`` opt-in rationale does not apply here.

        Pydantic's ``input`` and ``ctx`` are omitted -- the rejected value can
        be a credential, and the field path plus the reason is what a caller
        can act on.
        """
        errors = (
            exc.cause.errors(
                include_url=False, include_input=False, include_context=False
            )
            if isinstance(exc, RequestContractError)
            else []
        )
        detail = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
            for error in errors
        ]
        fields = ", ".join(item["field"] for item in detail if item["field"]) or "body"
        logger.warning(
            "Rejected a malformed request to %s for app %s: invalid field(s) %s",
            request.url.path,
            app_name,
            fields,
        )
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "message": f"Invalid request: {fields}",
                "detail": detail,
            },
        )

    if app_package is not None:
        # Opt-in only. Starlette routes an ``Exception`` handler to
        # ``ServerErrorMiddleware``, the one layer outside every user
        # middleware, so this is the only way the stamp reaches a response
        # synthesized from an unhandled exception — but installing it *also*
        # replaces Starlette's plain-text "Internal Server Error" with a JSON
        # envelope, for every app built by this function. An app that passes no
        # ``app_package`` has adopted nothing and must stay byte-identical on
        # its 500, so the trade is offered only to apps that opted in.

        @app.exception_handler(Exception)
        async def _stamped_server_error(
            request: Request, exc: Exception
        ) -> JSONResponse:
            """Own the 500 so it, too, carries the stamp.

            ``ServerErrorMiddleware`` re-raises after sending, so the traceback
            still reaches the process log; the client gets a generic body with
            no internals, matching the per-route handlers above.
            """
            logger.error(
                "Unhandled error for app %s on %s: %s",
                app_name,
                request.url.path,
                exc,
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                content=_wrap_response(
                    {}, message="Internal server error", success=False
                ),
                headers={
                    APP_VERSION_HEADER: header_safe(version),
                    SERVER_REVISION_HEADER: revision.as_header(),
                },
            )

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
        auth_input = validate_request(AuthInput, body)
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
        preflight_input = validate_request(PreflightInput, body)
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
        metadata_input = validate_request(MetadataInput, body)
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
            # Bytes, not text: json accepts them natively, so this drops the
            # platform-locale decode (P046) rather than papering over it.
            raw = json.loads(target.read_bytes())
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
        app_version=version,
        revision=revision,
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
    async def root() -> dict[str, Any]:
        return {
            "app": app_name or title,
            "sdk": SERVER_SDK_DIST,
            "app_version": version,
            "server_revision": revision.as_dict(),
        }

    return app
