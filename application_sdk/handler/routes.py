"""The handler HTTP surface: ``/workflows/v1/auth``, ``/check`` and ``/metadata``.

One implementation, registered by both the worker's handler service
(``application_sdk.handler.service``) and the consolidated API host
(:func:`application_sdk.build_asgi_app`), so an app's handler answers the
same way on either surface: the same request contract (422 on a malformed
body), credential normalisation, per-entrypoint dispatch, error boundary,
``AppError.category`` → HTTP status mapping, redaction and response envelope.

Moved verbatim from ``application_sdk.handler.service``; the differences are
the seams a thin package needs:

* ``app_package`` names the package holding per-entrypoint handler modules
  (``app`` on the worker, e.g. ``atlan_mysql_api`` on the host);
* preflight outcome rows go through a :class:`PreflightObserver` — the worker
  passes the preflight gate's emitters, the host logs;
* the request body is read with :func:`read_json_object`, so unparseable JSON or
  a non-object answers 422 instead of 500 on both surfaces.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, Protocol, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from application_sdk._logging import get_logger
from application_sdk.credentials.ingress import lift_agent_json
from application_sdk.errors import FailureCategory
from application_sdk.errors.base import AppError, safe_traceback, sanitize_cause_repr
from application_sdk.errors.leaves import InternalError
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.context import (
    HandlerContext,
    SecretStore,
    bind_handler_context,
)
from application_sdk.handler.contracts import (
    AuthInput,
    HandlerCredential,
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    normalize_credentials,
    unverifiable_preflight_result,
)
from application_sdk.handler.request_contract import (
    RequestContractError,
    read_json_object,
    validate_request,
)

logger = get_logger(__name__)


def entrypoint_module_segment(name: str) -> str:
    """Kebab-case entry-point name → its Python module segment (``a-b`` → ``a_b``).

    Same conversion as ``application_sdk.app.entrypoint.entrypoint_module_segment``.
    """
    return name.replace("-", "_")


_CATEGORY_TO_HTTP: dict[FailureCategory, int] = {
    FailureCategory.AUTH: 401,
    FailureCategory.PERMISSION: 403,
    FailureCategory.NOT_FOUND: 404,
    FailureCategory.ALREADY_EXISTS: 409,
    FailureCategory.INVALID_INPUT: 400,
    FailureCategory.PRECONDITION: 412,
    FailureCategory.RATE_LIMITED: 429,
    FailureCategory.TIMEOUT: 504,
    FailureCategory.SOURCE_UNAVAILABLE: 503,
    FailureCategory.DEPENDENCY_UNAVAILABLE: 503,
    FailureCategory.RESOURCE_EXHAUSTED: 503,
    FailureCategory.DATA_INTEGRITY: 500,
    FailureCategory.INTERNAL: 500,
    FailureCategory.UNIMPLEMENTED: 501,
    FailureCategory.CANCELLED: 499,  # client-closed-request (nginx convention)
}


def _app_error_to_http_status(exc: AppError) -> int:
    return _CATEGORY_TO_HTTP.get(exc.category, 500)


def _normalize_preflight_request(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize preflight-specific compatibility fields before validation."""
    normalized = normalize_credentials(lift_agent_json(body))
    # Fold the camelCase spelling onto the field name first: the mirror below
    # decides which block to copy by inspecting the raw body, so a caller that
    # sends only ``connectionConfig`` would otherwise get an empty ``metadata``.
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
    """One check as the HTTP caller sees it.

    ``cause_repr`` is the exception text behind a typed error. It stays in the
    server log and the Temporal payload; an HTTP caller gets the typed fields.
    """
    dumped = check.model_dump(
        mode="json", exclude_none=True, exclude={"error": {"cause_repr"}}
    )
    # The -1.0 "not measured" sentinel belongs to the telemetry row
    # (check_matrix), not to this display payload — the frontend should see
    # no duration rather than a negative one.
    if dumped.get("duration_ms", 0) < 0:
        del dumped["duration_ms"]
    dumped["message"] = check.resolved_message
    if check.resolved_suggested_action:
        dumped["suggested_action"] = check.resolved_suggested_action
    return dumped


def _preflight_response(
    result: PreflightOutput, *, success: bool | None = None
) -> dict[str, Any]:
    """The ``/workflows/v1/check`` body for a verdict.

    ``data`` is the v2 map the SageV2 widget iterates: one camelCase key per
    check with ``success`` and, because the widget renders
    ``checkResult.success ? successMessage : failureMessage`` with no fallback,
    both message fields (DBBI-665, WARE-1250). Envelope ``success`` means
    "preflight executed", not "every check passed": the widget short-circuits
    on ``!response.success`` and would otherwise render every PARTIAL or
    NOT_READY verdict as a blank failure. The canonical verdict lives under
    ``preflight``.
    """
    data: dict[str, Any] = {}
    for check in result.checks:
        key = check.name[0].lower() + check.name[1:]
        msg = check.resolved_message or ""
        data[key] = {
            "success": check.passed,
            "message": msg,
            "successMessage": msg if check.passed else "",
            "failureMessage": "" if check.passed else msg,
        }
    response = _wrap_response(
        data,
        message=result.message or f"Preflight check {result.status.value}",
        success=len(result.checks) > 0 if success is None else success,
    )
    response["preflight"] = _preflight_runtime_summary(result)
    return response


def _preflight_failure_response(
    exc: AppError, app_name: str, status_code: int, detail: str | None = None
) -> JSONResponse:
    """The ``/workflows/v1/check`` body when the handler raised instead of returning.

    A raise used to leave the caller with an HTTP status and a string. It now
    carries the same verdict shape a returned ``NOT_READY`` does, built the way
    the gate builds it: ``status`` is ``not_ready`` and one ``preflightVerdict``
    check carries the raise as typed ``FailureDetails`` — the leaf's own for a
    typed raise, ``InternalError`` with ``classification_pending`` for a crash.
    So the status says the source was not verified while the check says who
    must act. The HTTP status is unchanged. ``detail`` is the typed leaf's own
    message, never ``str(exc)``: for an ``AppError`` that is the same string as
    before, for the deprecated ``HandlerError`` it drops the ``[CODE]`` prefix
    and the ``handler=`` / ``app=`` suffix, which stay in the server log. The
    raw exception text never reaches the body: ``cause_repr`` is dropped before
    the verdict is rendered, because after secret redaction it still names the
    caller's hosts and accounts, and the untyped path passes a fixed ``detail``.
    """
    output = unverifiable_preflight_result(exc, app_name, include_cause=False)
    body = _preflight_response(output, success=False)
    failure = output.checks[0].error
    body["detail"] = detail if detail is not None else output.message
    body["error"] = (
        failure.model_dump(mode="json", exclude_none=True)
        if failure is not None
        else None
    )
    return JSONResponse(status_code=status_code, content=body)


def _preflight_runtime_summary(result: PreflightOutput) -> dict[str, Any]:
    """Runtime metadata kept outside the SageV2 ``data`` map.

    ``status`` is the gate verdict (``ready`` / ``not_ready`` / ``partial``);
    ``not_ready`` means blocked. Per-check ``message``/``suggested_action`` follow
    the precedence rule (typed ``error`` wins). Consumed for display/diagnostics.
    """
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
    """Wrap response data in the standard envelope: {success, message, data}.

    ``message`` is omitted from the response when empty to match the legacy
    /credentials/query format expected by the frontend filter widgets.
    """
    result: dict[str, Any] = {"success": success, "data": data}
    if message:
        result["message"] = message
    return result


_ENTRYPOINT_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")

HandlerFn = Callable[[Any, HandlerContext], Awaitable[Any]]


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


def _discover_handler_fn(
    entrypoint: str, app_package: str, fn_name: str
) -> HandlerFn | None:
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

    segment = entrypoint_module_segment(entrypoint)
    module = _import_optional_app_module(f"{app_package}.{segment}.handler")
    if module is None:
        return None
    fn = getattr(module, fn_name, None)
    # The dispatch ``await``s the result, so require a coroutine function — a
    # sync ``def`` falls through to the app-level Handler rather than blowing up
    # with a TypeError at request time.
    if fn is not None and not inspect.iscoroutinefunction(fn):
        logger.debug(
            "%s.%s.%s found but not async; falling through to app-level Handler",
            app_package,
            segment,
            fn_name,
        )
    return cast("HandlerFn | None", fn if inspect.iscoroutinefunction(fn) else None)


class PreflightObserver(Protocol):
    """Where the ``/check`` route reports a verdict or a crash.

    The worker's handler service passes one backed by the preflight gate's
    ``emit_preflight_check_outcome`` / ``emit_preflight_crash_outcome`` (the setup
    funnel's rows). The API host has no gate and uses :class:`LoggingPreflightObserver`.
    """

    def outcome(
        self, result: PreflightOutput, *, entrypoint: str, request_id: str
    ) -> None: ...

    def crash(
        self, exc: BaseException, *, entrypoint: str, request_id: str
    ) -> None: ...


class LoggingPreflightObserver:
    """Default observer: one structured log line per verdict or crash."""

    def __init__(self, app_name: str) -> None:
        self._app_name = app_name

    def outcome(
        self, result: PreflightOutput, *, entrypoint: str, request_id: str
    ) -> None:
        logger.info(
            "Preflight check outcome: app=%s entrypoint=%s request=%s status=%s",
            self._app_name,
            entrypoint or "<implicit>",
            request_id,
            result.status.value,
        )

    def crash(self, exc: BaseException, *, entrypoint: str, request_id: str) -> None:
        logger.warning(
            "Preflight check crashed: app=%s entrypoint=%s request=%s error=%s",
            self._app_name,
            entrypoint or "<implicit>",
            request_id,
            type(exc).__name__,
        )


def register_handler_routes(
    app: FastAPI,
    handler: Handler,
    *,
    app_name: str,
    app_package: str = "app",
    secret_store: SecretStore | None = None,
    observer: PreflightObserver | None = None,
    include_app_routers: bool = True,
) -> None:
    """Register the auth / check / metadata routes and the 422 contract handler on ``app``.

    Args:
        app: The FastAPI app to register on.
        handler: The app's :class:`Handler` instance (``DefaultHandler`` if none).
        app_name: The app's name, stamped on the request context and every log line.
        app_package: Package holding per-entrypoint ``<segment>.handler`` modules.
        secret_store: Backs ``HandlerContext.get_secret``; ``None`` on the API host.
        observer: Receives ``/check`` verdicts and crashes; logs when ``None``.
        include_app_routers: Also serve ``handler.routers()`` now. A caller that
            registers more SDK routes afterwards passes ``False`` and calls
            :func:`include_app_routers` last, so the collision check sees them.
    """
    _secret_store = secret_store
    observer = observer or LoggingPreflightObserver(app_name)

    @app.exception_handler(RequestContractError)
    async def _handle_request_contract_error(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Turn a request-body contract failure into a 422 naming the field.

        Pydantic's ``input`` and ``ctx`` are omitted -- the rejected value can
        be a credential, and the field path plus the reason is what a caller
        can act on.
        """
        errors = (
            exc.cause.errors(
                include_url=False, include_input=False, include_context=False
            )
            if isinstance(exc, RequestContractError) and exc.cause is not None
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
            # %r on the path: it is caller-controlled and percent-decoded, so a
            # %0A in it would forge a log line. No exc_info here, so this record
            # is single-line and a forged one would be indistinguishable.
            "Rejected a malformed request to %r for app %s: invalid field(s) %s",
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
        body = normalize_credentials(lift_agent_json(await read_json_object(request)))
        auth_input = validate_request(AuthInput, body)
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
                    _discover_handler_fn(entrypoint, app_package, "test_auth")
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
                        result.model_dump(
                            mode="json", exclude={"error": {"cause_repr"}}
                        ),
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
                # conformance: ignore[L009] boundary handler logs the redacted exception and traceback plus request_id then raises a sanitized HTTPException `from None`; the log is the only server-side record.
                logger.error(
                    "Auth test failed for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
                )
                raise HTTPException(status_code=e.http_status, detail=str(e)) from None
            except AppError as e:
                # Forward-looking: typed AppError leaves from connectors that raise
                # non-HandlerError typed errors (already migrated).
                # conformance: ignore[L009] boundary handler logs the redacted exception and traceback plus request_id then raises a sanitized HTTPException `from None`; the log is the only server-side record.
                logger.error(
                    "Auth test failed for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
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
                # conformance: ignore[L009] boundary handler logs the redacted exception and traceback plus request_id then raises a sanitized HTTPException `from None`; the log is the only server-side record.
                logger.error(
                    "Auth test failed unexpectedly for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
                )
                raise HTTPException(
                    status_code=500, detail="Internal server error"
                ) from None

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/check")
    async def preflight_check(request: Request) -> JSONResponse:
        body = _normalize_preflight_request(await read_json_object(request))
        preflight_input = validate_request(PreflightInput, body)
        credentials = [
            HandlerCredential(key=c.key, value=c.value)
            for c in preflight_input.credentials
        ]
        context = _create_context(credentials)
        with bind_handler_context(context):

            def _crash_row(e: BaseException) -> None:
                observer.crash(
                    e, entrypoint=entrypoint, request_id=context.request_id_str
                )

            # Seeded from the *requested* value, not "", so a raise before
            # validation still names what the caller asked for — an empty
            # seed would be stamped as "<implicit>" and misattribute the row.
            entrypoint = preflight_input.entrypoint or ""
            try:
                logger.info(
                    "Preflight check started: app=%s request=%s",
                    app_name,
                    context.request_id_str,
                )
                # Per-entrypoint dispatch (see test_auth above for rationale).
                entrypoint = _validated_entrypoint(preflight_input.entrypoint)
                ep_fn = (
                    _discover_handler_fn(entrypoint, app_package, "preflight_check")
                    if entrypoint
                    else None
                )
                if ep_fn is not None:
                    result = await ep_fn(preflight_input, context)
                else:
                    result = await handler.preflight_check(preflight_input)
                observer.outcome(
                    result, entrypoint=entrypoint, request_id=context.request_id_str
                )
                return JSONResponse(content=_preflight_response(result))
            except HandlerError as e:
                # TODO(signal-over-noise): [P13] Deprecated path — HandlerError is an
                # AppError subclass caught here first so http_status is preserved.
                # Remove once all connector subclasses raise typed AppError leaves.
                # Tracked alongside the Handler abstract-method contract migration.
                # See typed-error-prescription.md §5 (HandlerError row).
                logger.error(
                    "Preflight check failed for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
                )
                _crash_row(e)
                return _preflight_failure_response(e, app_name, e.http_status)
            except AppError as e:
                logger.error(
                    "Preflight check failed for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
                )
                _crash_row(e)
                return _preflight_failure_response(
                    e, app_name, _app_error_to_http_status(e)
                )
            except HTTPException as e:
                # Deliberate client-facing responses (e.g. 400 from a malformed
                # entrypoint name) pass through unrecorded — the response *is*
                # the channel, so a row would double-count what the caller can
                # already see. A 5xx raised this way is a crash wearing an HTTP
                # status: it reaches none of the boundary handlers around it, so
                # without this it drops out of the setup funnel's denominator —
                # the same hole on this surface that CONNECT-1170 gap 3 closed
                # for handler raises.
                if e.status_code >= 500:
                    _crash_row(e)
                raise
            except Exception as e:
                # conformance: ignore[L009] boundary handler logs the real exception plus request_id (exc_info); the response carries a fixed message, never the exception text.
                logger.error(
                    "Preflight check failed unexpectedly for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
                )
                _crash_row(e)
                return _preflight_failure_response(
                    InternalError(
                        message="Preflight could not be verified due to an internal error.",
                        app_name=app_name,
                        cause=e,
                        retryable=False,
                        component="preflight_handler",
                        classification_pending=True,
                    ),
                    app_name,
                    500,
                    "Internal server error",
                )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @app.post("/workflows/v1/metadata")
    async def fetch_metadata(request: Request) -> JSONResponse:
        body = normalize_credentials(lift_agent_json(await read_json_object(request)))
        metadata_input = validate_request(MetadataInput, body)
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
                    _discover_handler_fn(entrypoint, app_package, "fetch_metadata")
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
                data = [
                    obj.model_dump() if hasattr(obj, "model_dump") else obj
                    for obj in result.objects
                ]
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
                # conformance: ignore[L009] boundary handler logs the redacted exception and traceback plus request_id then raises a sanitized HTTPException `from None`; the log is the only server-side record.
                logger.error(
                    "Metadata fetch failed for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
                )
                raise HTTPException(status_code=e.http_status, detail=str(e)) from None
            except AppError as e:
                # Forward-looking: typed AppError leaves from connectors that raise
                # non-HandlerError typed errors (already migrated).
                # conformance: ignore[L009] boundary handler logs the redacted exception and traceback plus request_id then raises a sanitized HTTPException `from None`; the log is the only server-side record.
                logger.error(
                    "Metadata fetch failed for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
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
                # conformance: ignore[L009] boundary handler logs the redacted exception and traceback plus request_id then raises a sanitized HTTPException `from None`; the log is the only server-side record.
                logger.error(
                    "Metadata fetch failed unexpectedly for app %s (request %s): %s\n%s",
                    app_name,
                    context.request_id_str,
                    sanitize_cause_repr(e),
                    safe_traceback(e),
                )
                raise HTTPException(
                    status_code=500, detail="Internal server error"
                ) from None

    if include_app_routers:
        include_app_routers_on(app, handler, app_name)


def include_app_routers_on(app: FastAPI, handler: Handler, app_name: str) -> None:
    """Serve the routers ``handler.routers()`` returns, after the SDK's own routes.

    This is how an app adds endpoints of its own. The worker's handler service
    and the API host both come here, so an app route is served identically in
    both places, and the host needs no knowledge of it. A router that redefines
    a path and method the SDK already serves is refused at startup: it would
    otherwise be shadowed on one surface and not the other.
    """
    routers = list(handler.routers())
    if not routers:
        return
    taken = {
        (getattr(r, "path", ""), method)
        for r in app.routes
        for method in (getattr(r, "methods", None) or ())
    }
    for router in routers:
        for route in router.routes:
            path = f"{router.prefix}{getattr(route, 'path', '')}"
            for method in getattr(route, "methods", None) or ():
                if (path, method) in taken:
                    raise ValueError(
                        f"{app_name}: handler router redefines {method} {path}, "
                        "which the SDK already serves"
                    )
        app.include_router(router)
