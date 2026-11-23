"""The config and file-upload routes, over the app's object store.

``/workflows/v1/config/{id}`` (GET and POST) and ``/workflows/v1/file`` read and
write the tenant object store. Heracles writes a credential's non-secret fields
through ``POST /workflows/v1/config/{guid}?type=credentials`` on credential
create and update, and on every run submit before it fetches the manifest; the
app's worker reads them back at run time. ``/workflows/v1/file`` stores a file
from a setup form's file field. The worker's handler service and the
consolidated API host register the same routes through
:func:`register_object_routes`, and differ only in the store behind them:

* the worker uses its Dapr-configured ``application_sdk.storage`` store;
* the host has no Dapr, so :meth:`HostObjectStore.from_env` builds an obstore
  store (the ``obstore`` extra) from the chart-injected aliases, with the same
  constructors the worker uses (:mod:`application_sdk._obstore_config`): S3 from
  ``ATLAN_S3_BUCKET`` / ``ATLAN_S3_REGION`` with the pod's ambient AWS identity;
  on GCP, GCS from ``ATLAN_GCP_BUCKET`` through its S3-compatible API with the
  chart's HMAC keys; on an Azure Blob tenant, the container from
  ``ATLAN_AZURE_STORAGE_ACCOUNT`` / ``ATLAN_AZURE_STORAGE_CONTAINER``. Without
  any, the routes answer 503, exactly as the worker does without a store.

Both write a config to ``persistent-artifacts/apps/{app_name}/{type}/{id}/config.json``.
On the host ``app_name`` is the hosted app's name, never the host's own, so the
key is the one that app's worker reads.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shutil
import tempfile
import warnings
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Protocol, cast
from uuid import uuid4

import orjson
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    params,
)
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from typing_extensions import deprecated

from application_sdk import _env, _obstore_config
from application_sdk._logging import get_logger
from application_sdk._runtime.offload import run_in_thread
from application_sdk.handler.contracts import FileUploadResponse
from application_sdk.handler.routes import _wrap_response
from application_sdk.handler.service_errors import (
    InvalidConfigIdError,
    InvalidConfigTypeError,
    TempPathEscapeError,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore

logger = get_logger(__name__)

# Allowed characters for config_id and config_type path components. Prevents
# path traversal (no slashes, dots, or percent-encoding) when these values are
# interpolated into object-store keys.
CONFIG_KEY_PATTERN = r"^[a-zA-Z0-9_\-]{1,128}$"
_CONFIG_KEY_RE = re.compile(CONFIG_KEY_PATTERN)

# An upload prefix: one or more '/'-separated segments of letters, digits, '_',
# '-' and '.', none of them '.' or '..', with no leading, trailing or doubled
# '/'. It comes from the browser (Heracles forwards the form field unchanged), so
# it is untrusted input that becomes part of an object-store key.
_UPLOAD_PREFIX_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.\-]+")
_MAX_UPLOAD_PREFIX = 256


def valid_upload_prefix(prefix: str) -> bool:
    """Whether *prefix* is a safe relative object-store prefix (see above)."""
    if not prefix or len(prefix) > _MAX_UPLOAD_PREFIX:
        return False
    return all(
        segment not in (".", "..") and _UPLOAD_PREFIX_SEGMENT_RE.fullmatch(segment)
        for segment in prefix.split("/")
    )


#: First segments a worker refuses in an upload prefix: the object-store trees
#: the SDK itself writes, in the bucket every app on the tenant shares. Apps'
#: configs and state live under ``persistent-artifacts/apps/<app>/``, run
#: outputs under ``artifacts/apps/<app>/``, and connector caches under
#: ``connection-cache/``, each listed and read back by its owner. The host needs
#: no such list: it puts every upload under the app's own namespace.
_RESERVED_UPLOAD_ROOTS = frozenset(
    {"persistent-artifacts", "artifacts", "connection-cache"}
)


# Strips any character that is not a simple alphanumeric from file extensions
# before they become part of an object-store key.
SAFE_EXT_RE = re.compile(r"[^a-zA-Z0-9]")


def config_key(app_name: str, config_id: str, config_type: str = "workflows") -> str:
    """The object-store key of one app's config (the v2 statestore convention).

    ``persistent-artifacts/apps/{app_name}/{type}/{id}/config.json``.
    """
    if not _CONFIG_KEY_RE.match(config_id):
        raise InvalidConfigIdError(config_id=config_id)
    if not _CONFIG_KEY_RE.match(config_type):
        raise InvalidConfigTypeError(config_type=config_type)
    return f"persistent-artifacts/apps/{app_name}/{config_type}/{config_id}/config.json"


def validated_temp_path(path: str) -> str:
    """Resolve *path* and require it to lie strictly inside the system temp directory.

    Every temp path here comes from :func:`tempfile.mkstemp`; this makes the
    invariant explicit so SAST tools can confirm no user-supplied value can
    escape the temp directory. The temp directory itself is rejected;
    :func:`tempfile.mkstemp` never returns it. Returns the resolved path. The
    worker's handler service uses this same guard.
    """
    tmp_root = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(path)
    if not real.startswith(tmp_root + os.sep):
        raise TempPathEscapeError()
    return real


@dataclass
class ObjectRoutesSource:
    """The store operations the routes need, for one app.

    Args:
        configured: Whether a store is configured. ``False`` makes every route
            answer 503.
        load_config: ``(config_id, config_type)`` → the stored config, or
            ``None`` when it is missing. Raises when the store cannot answer
            (the route answers 503).
        save_config: ``(config_id, body, config_type)`` → ``True`` once stored,
            ``False`` when no store is configured. Raises on a failed write.
        upload_file: ``(key, local_path)`` → stores the file at ``key``.
        upload_namespace: When set, every upload key is placed under it. The
            consolidated host sets the app's own
            ``persistent-artifacts/apps/<app>/uploads``: one identity writes to
            the shared bucket for every hosted app, so a request routed to one
            app must not be able to write under another's prefix. When unset
            (the worker), the prefix is kept as given but may not start with
            one of :data:`_RESERVED_UPLOAD_ROOTS`. Either way an unsafe prefix
            (see :func:`valid_upload_prefix`) answers 400.
    """

    configured: Callable[[], bool]
    load_config: Callable[[str, str], Awaitable[dict[str, Any] | None]]
    save_config: Callable[[str, dict[str, Any], str], Awaitable[bool]]
    upload_file: Callable[[str, str], Awaitable[None]]
    upload_namespace: str = ""


#: Path of the SDK's setup-form file upload route.
UPLOAD_ROUTE = "/workflows/v1/file"

#: Default cap on a request body a route reads whole (4 MiB): every route but a
#: form route. FastAPI reads such a body into memory before it parses it, so
#: this bounds what one request can make the process hold. The largest setup
#: form we ship decodes to ~5 KB.
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
#: Default cap on a body sent to a form route (512 MiB): a route that declares a
#: ``File`` or ``Form`` parameter, such as :data:`UPLOAD_ROUTE` or an app's own
#: upload route. Starlette spools a multipart file to local disk while parsing
#: it, before the route runs, so this bounds the ephemeral storage one upload
#: can take.
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024

MAX_BODY_BYTES_ENV = "ATLAN_HANDLER_MAX_BODY_BYTES"
MAX_UPLOAD_BYTES_ENV = "ATLAN_HANDLER_MAX_UPLOAD_BYTES"

_TOO_LARGE_DETAIL = "Request body too large"
_TOO_LARGE_BODY = orjson.dumps({"detail": _TOO_LARGE_DETAIL})


def _too_large() -> HTTPException:
    """The signal ``receive`` raises once a body passes its cap.

    An ``HTTPException`` because every layer between the read and the response
    already passes one through untouched and unlogged: the routes' error
    boundaries re-raise it, FastAPI's form parsing re-raises it, and
    ``ExceptionMiddleware`` renders it as the 413. It is not a ``ValueError``,
    so :func:`~application_sdk.handler.request_contract.read_json_object` does
    not answer 422 for it.
    """
    return HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)


def _report_env(message: str) -> None:
    logger.warning("Ignoring env var: %s", message)


class RequestBodyLimitMiddleware:
    """Answer 413 to a request whose body is larger than its cap.

    Pure ASGI, so it counts bytes as they arrive and never buffers a body. A
    ``Content-Length`` over the cap is refused before anything is read. A body
    without one, or with one that understates it, is refused when the running
    count passes the cap: ``receive`` raises a 413 ``HTTPException``, which
    stops a multipart parser before it spools more to disk (Starlette closes the
    spooled files when the read fails). Should an inner layer answer something
    else, the response is still replaced by the 413, unless the app had already
    started its response.

    The cap depends on the route that reads the body. Routing runs before any
    read and records the route in ``scope["route"]``, so the cap is chosen at
    the first ``receive``, before a byte is counted. A form route (one that
    declares a ``File`` or ``Form`` parameter) streams its body to disk and gets
    ``max_upload_bytes``. Every other route, a mount included, may read its body
    whole into memory and gets ``max_body_bytes``. The route decides, not the
    request's ``Content-Type``: FastAPI reads a non-form body whole before it
    looks at the content type, so a large body labelled multipart must still
    meet the smaller cap on a JSON route. An app's own upload route gets the
    upload cap by declaring its file parameter, with nothing to configure. A
    route that calls ``request.form()`` by hand gets the smaller cap.

    A declared ``Content-Length`` over both caps is refused before routing; one
    over the route's cap is refused at the first ``receive``, with nothing read.

    :func:`install_body_limit` puts it inside every other user middleware, so
    the raise never crosses a ``BaseHTTPMiddleware`` (the worker's
    ``LogMiddleware``): that would wrap it in an ``ExceptionGroup`` no handler
    matches and log an expected rejection as a failed request.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        max_upload_bytes: int,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_upload_bytes = max_upload_bytes

    def _limit_for(self, scope: Scope) -> int:
        """The cap of the route routing chose (``max_body_bytes`` when none)."""
        if _is_form_route(scope.get("route")):
            return self.max_upload_bytes
        return self.max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        largest = max(self.max_body_bytes, self.max_upload_bytes)
        if declared is not None and declared > largest:
            # No route takes a body this size: refuse it before routing.
            self._log_rejection(scope, largest)
            await _send_413(send)
            return

        limit: int | None = None
        received = 0
        exceeded = False
        response_started = False
        replaced = False

        async def limited_receive() -> Message:
            nonlocal limit, received, exceeded
            if exceeded:
                raise _too_large()
            if limit is None:
                limit = self._limit_for(scope)
                if declared is not None and declared > limit:
                    exceeded = True
                    raise _too_large()
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _too_large()
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started, replaced
            if replaced:
                # The inner app's answer to the oversize body is discarded.
                return
            if message["type"] == "http.response.start":
                if exceeded:
                    replaced = True
                    await _send_413(send)
                    return
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # Once the cap has tripped, whatever escaped is its consequence:
            # the 413 itself, or an inner layer's wrapping of it.
            if not exceeded or response_started:
                raise
        if exceeded:
            if not replaced and not response_started:
                await _send_413(send)
            self._log_rejection(scope, limit or self.max_body_bytes)

    @staticmethod
    def _log_rejection(scope: Scope, limit: int) -> None:
        logger.warning(
            "Request body over the %d-byte limit on %s %s; answered 413",
            limit,
            scope.get("method", ""),
            scope.get("path", ""),
        )


def _is_form_route(route: object) -> bool:
    """Whether *route* parses its body as a form (a ``File`` or ``Form`` parameter).

    FastAPI parses such a body with ``request.form()``, which spools files to
    disk, and reads any other body whole. ``File`` subclasses ``Form``.
    """
    if not isinstance(route, APIRoute) or route.body_field is None:
        return False
    return isinstance(route.body_field.field_info, params.Form)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_413(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_TOO_LARGE_BODY)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _TOO_LARGE_BODY})


def install_body_limit(app: FastAPI) -> None:
    """Cap request bodies on *app*, reading the caps from the environment.

    :envvar:`ATLAN_HANDLER_MAX_UPLOAD_BYTES` caps a body sent to a form route
    (``/workflows/v1/file``, or an app's own route with a ``File`` parameter),
    :envvar:`ATLAN_HANDLER_MAX_BODY_BYTES` every other body. Called
    by :func:`register_object_routes`, which the worker and the consolidated host
    both call, so neither app can be built without the cap. Idempotent.

    Appended, not added with ``add_middleware`` (which makes a middleware the
    outermost): the cap must run inside every other user middleware, whenever
    that was added. See :class:`RequestBodyLimitMiddleware`.
    """
    if getattr(app.state, "body_limit_installed", False):
        return
    if app.middleware_stack is not None:
        raise RuntimeError("Cannot add middleware after an application has started")
    limit = Middleware(
        RequestBodyLimitMiddleware,
        max_body_bytes=_env.env_int(
            MAX_BODY_BYTES_ENV, DEFAULT_MAX_BODY_BYTES, minimum=1, report=_report_env
        ),
        max_upload_bytes=_env.env_int(
            MAX_UPLOAD_BYTES_ENV,
            DEFAULT_MAX_UPLOAD_BYTES,
            minimum=1,
            report=_report_env,
        ),
    )
    app.user_middleware.append(limit)
    app.state.body_limit_installed = True


def register_object_routes(app: FastAPI, source: ObjectRoutesSource) -> None:
    """Register the config and file-upload routes on *app*.

    Also installs the request-body cap for the whole app
    (:func:`install_body_limit`): every route's body is bounded, and an upload is
    refused with 413 before it fills the pod's disk.
    """
    install_body_limit(app)

    @app.get("/workflows/v1/config/{config_id}")
    async def get_workflow_config(
        config_id: Annotated[str, PathParam(pattern=CONFIG_KEY_PATTERN)],
        type: Annotated[str, Query(pattern=CONFIG_KEY_PATTERN)] = "workflows",
    ) -> JSONResponse:
        """Fetch workflow config from object store."""
        try:
            config = await source.load_config(config_id, type)
        except Exception as exc:
            # A missing key loads as None (404 below). Anything else (IAM,
            # network, a 5xx, an unreadable object) means the store could not
            # answer, which is not the same as the config being absent.
            logger.warning(
                "Object-store config load failed for config_id=%s type=%s",
                config_id,
                type,
                exc_info=True,
            )
            raise HTTPException(
                status_code=503, detail="Object store unavailable"
            ) from exc

        if config is None and not source.configured():
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
        config_id: Annotated[str, PathParam(pattern=CONFIG_KEY_PATTERN)],
        request: Request,
        type: Annotated[str, Query(pattern=CONFIG_KEY_PATTERN)] = "workflows",
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

        saved = await source.save_config(config_id, body, type)

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

    @app.post(UPLOAD_ROUTE)
    async def upload_file(
        file: UploadFile = File(...),
        filename: str | None = Form(None),
        prefix: str | None = Form(None),
        contentType: str | None = Form(None),
    ) -> JSONResponse:
        if not source.configured():
            raise HTTPException(status_code=503, detail="Storage not configured")

        raw_name = filename or file.filename or "upload"
        # Strip non-alphanumeric chars and cap at 16 chars for object-store key safety.
        extension = SAFE_EXT_RE.sub("", PurePosixPath(raw_name).suffix.lstrip("."))[:16]
        resolved_type = (
            contentType
            or file.content_type
            or (mimetypes.guess_type(raw_name)[0] if raw_name else None)
            or "application/octet-stream"
        )

        file_id = str(uuid4())
        effective_prefix = prefix or "workflow_file_upload"
        if not valid_upload_prefix(effective_prefix):
            raise HTTPException(
                status_code=400,
                detail="prefix must be relative path segments of letters, "
                "digits, '_', '-' or '.', with no '.' or '..' segment",
            )
        if source.upload_namespace:
            # The consolidated host: one AWS identity and one bucket serve every
            # hosted app, so every upload goes under the app's own namespace.
            effective_prefix = f"{source.upload_namespace}/{effective_prefix}"
        elif effective_prefix.split("/", 1)[0] in _RESERVED_UPLOAD_ROOTS:
            # A worker keeps the prefix as given, so its keys stay where its
            # readers expect them (``workflow_file_upload/...``), but the bucket
            # is shared by every app on the tenant: a prefix must not plant an
            # object in a tree the SDK writes and other apps or runs list.
            raise HTTPException(
                status_code=400,
                detail="prefix must not start with a reserved object-store root",
            )
        stored_name = f"{file_id}.{extension}" if extension else file_id
        key = f"{effective_prefix}/{stored_name}"

        # Stream the upload: drain the spooled temp file to a named temp file,
        # then stream-upload to the object store (avoids materialising in memory).
        # The temp name carries nothing from the request: the extension belongs
        # to the object key only, and the store never reads the local suffix.
        fd, tmp_path = tempfile.mkstemp(suffix=".upload")
        safe_tmp_path = validated_temp_path(tmp_path)
        try:
            os.close(fd)

            def _drain_to_tmp() -> int:
                with open(safe_tmp_path, "wb") as dst:
                    shutil.copyfileobj(file.file, dst)
                return os.path.getsize(safe_tmp_path)

            file_size = await run_in_thread(_drain_to_tmp)
            await source.upload_file(key, safe_tmp_path)
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


def _normalize_key(key: str) -> str:
    """A clean relative object-store key, as the worker's store normalises one."""
    normalized = key.replace("\\", "/").strip("/")
    return "" if normalized == "." else normalized


class ObjectStoreOps(Protocol):
    """The three store calls :func:`host_object_source` makes."""

    async def get_json(self, key: str) -> dict[str, Any] | None:
        """The JSON object at ``key``, or ``None`` when it is missing."""
        ...

    async def put_json(self, key: str, body: dict[str, Any]) -> None:
        """Store ``body`` as JSON at ``key``."""
        ...

    async def put_file(self, key: str, local_path: str) -> None:
        """Store the local file at ``local_path`` under ``key``."""
        ...


@deprecated(
    "application_sdk.handler.object_routes.S3Client is deprecated and will be "
    "removed in v4.0.0 with S3ObjectStore. Use HostObjectStore, over obstore, "
    "instead."
)
class S3Client(Protocol):
    """The boto3 S3 client calls :class:`S3ObjectStore` makes.

    .. deprecated:: 3.57
        :class:`S3ObjectStore` is deprecated. Will be removed in v4.0.0.

    A Protocol rather than the ``mypy_boto3_s3`` client type: boto3 is the
    ``aws`` extra, so it is never imported at module level here.
    """

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str
    ) -> object: ...

    def upload_file(self, filename: str, bucket: str, key: str, /) -> None: ...


#: GCS's S3-compatible (XML API) endpoint.
_GCS_ENDPOINT = "https://storage.googleapis.com"


def _resolved_env(name: str) -> str:
    """``name``'s value, or ``""`` when unset or an unresolved Kubernetes alias.

    The host's manifest aliases chart names (``ATLAN_S3_BUCKET: "$(S3_BUCKET)"``);
    where the chart does not inject the source, Kubernetes leaves the
    ``$(...)`` text in place, which is no value, not a bucket or region.
    """
    value = os.environ.get(name, "").strip()
    return "" if value.startswith("$(") else value


@deprecated(
    "application_sdk.handler.object_routes.S3ObjectStore is deprecated and will be "
    "removed in v4.0.0. Use HostObjectStore.from_env() instead: it builds the "
    "store over obstore, on S3, GCS and Azure Blob; pass it to "
    "build_asgi_app(object_store=...)."
)
class S3ObjectStore:
    """One S3 bucket through a boto3 client: the host's former store.

    .. deprecated:: 3.57
        Use :class:`HostObjectStore`. Will be removed in v4.0.0.

    On AWS auth is ambient (node role / IRSA / env), exactly as the fleet's Dapr
    ``bindings.aws.s3`` components authenticate: their specs carry only a
    bucket and a region. On GCP the bucket is GCS, reached with the chart's HMAC
    keys (:meth:`from_env`); they go to boto3 and are never logged. boto3 calls
    run on the app's own ``run_in_thread`` pool, so S3 latency never blocks the
    host's event loop.
    """

    def __init__(
        self, bucket: str, region: str | None = None, client: S3Client | None = None
    ):
        if client is None:
            import boto3  # noqa: PLC0415 — the aws extra; loaded only when S3 is configured

            client = cast("S3Client", boto3.client("s3", region_name=region or None))
        self._client: S3Client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls) -> S3ObjectStore | None:
        """From ``ATLAN_S3_BUCKET`` / ``ATLAN_S3_REGION``, or on GCP from
        ``ATLAN_GCP_BUCKET`` (:meth:`_gcs_from_env`); ``None`` when no bucket is set."""
        gcs_bucket = _resolved_env("ATLAN_GCP_BUCKET")
        if gcs_bucket:
            # First: the chart injects no S3_* on GCP, so the host's ATLAN_S3_*
            # aliases are unresolved there.
            return cls._gcs_from_env(gcs_bucket)
        # Azure Blob tenants get no S3_* either: no bucket (Blob is not served yet).
        bucket = _resolved_env("ATLAN_S3_BUCKET")
        if not bucket:
            return None
        try:
            return cls(bucket, _resolved_env("ATLAN_S3_REGION") or None)
        except ModuleNotFoundError:
            logger.warning(
                "ATLAN_S3_BUCKET is set but boto3 is not installed, so the config and "
                "file routes answer 503. Install atlan-application-sdk-api[aws].",
                exc_info=True,
            )
            return None

    @classmethod
    def _gcs_from_env(cls, bucket: str) -> S3ObjectStore | None:
        """GCS through its S3-compatible API, with the tenant's HMAC key pair.

        The host's manifest aliases the atlan-app chart's GCP-only ``GCP_*``
        names as ``ATLAN_GCP_*``; elsewhere they are unresolved, so unset.
        """
        access_key = _resolved_env("ATLAN_GCP_HMAC_ACCESS_KEY")
        secret = _resolved_env("ATLAN_GCP_HMAC_SECRET")
        if not (access_key and secret):
            logger.warning(
                "ATLAN_GCP_BUCKET is set without ATLAN_GCP_HMAC_ACCESS_KEY / "
                "ATLAN_GCP_HMAC_SECRET, so the config and file routes answer 503."
            )
            return None
        try:
            import boto3  # noqa: PLC0415 — the aws extra; loaded only when GCS is configured
            from botocore.config import Config  # noqa: PLC0415 — ships with boto3
        except ModuleNotFoundError:
            logger.warning(
                "ATLAN_GCP_BUCKET is set but boto3 is not installed, so the config and "
                "file routes answer 503. Install atlan-application-sdk-api[aws].",
                exc_info=True,
            )
            return None
        client = boto3.client(
            "s3",
            endpoint_url=_GCS_ENDPOINT,
            region_name=_resolved_env("ATLAN_GCP_REGION") or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret,
            # boto3 >= 1.36 sends CRC32 checksums (aws-chunked uploads, checksum
            # mode on reads) that GCS rejects: boto/boto3#4400.
            config=Config(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return cls(bucket, client=cast("S3Client", client))

    async def get_json(self, key: str) -> dict[str, Any] | None:
        """The object at ``key``, or ``None`` when it is missing.

        Any other failure (IAM, network, throttling, an unreadable object)
        raises: the store could not answer, which is not a missing object.
        """

        def _get() -> bytes | None:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=key)
            # conformance: ignore[E004] only the not-found codes return None (the absent-object answer); everything else is re-raised
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code in ("NoSuchKey", "404"):
                    return None
                raise
            return response["Body"].read()

        raw = await run_in_thread(_get)
        return None if raw is None else orjson.loads(raw)

    async def put_json(self, key: str, body: dict[str, Any]) -> None:
        await run_in_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=orjson.dumps(body),
            ContentType="application/json",
        )

    async def put_file(self, key: str, local_path: str) -> None:
        await run_in_thread(
            self._client.upload_file, local_path, self._bucket, _normalize_key(key)
        )


class HostObjectStore:
    """One bucket or container through obstore: the host's store.

    The store comes from the constructors the worker uses
    (:mod:`application_sdk._obstore_config`), so the host gets the worker's
    timeouts, retries, proxy and custom-CA settings. obstore runs its requests
    on its own Rust runtime, so store latency never blocks the host's event
    loop. Credentials stay inside the obstore store; ``repr`` names only the
    kind of store.
    """

    def __init__(self, store: ObjectStore, *, kind: str = "obstore") -> None:
        self._store = store
        self._kind = kind

    def __repr__(self) -> str:
        return f"HostObjectStore({self._kind})"

    @classmethod
    def from_env(cls) -> HostObjectStore | None:
        """The store the host's env names, or ``None`` when it names none.

        A bucket wins over an Azure account, as in the chart: GCS from
        ``ATLAN_GCP_BUCKET`` (the chart injects no ``S3_*`` on GCP, so the
        host's ``ATLAN_S3_*`` aliases are unresolved there), else S3 from
        ``ATLAN_S3_BUCKET`` (also an Azure tenant with S3 enabled), else Azure
        Blob from ``ATLAN_AZURE_STORAGE_ACCOUNT``. An incomplete setting, a
        missing ``obstore`` extra or a store obstore refuses to build is logged
        and gives ``None``: the routes answer 503, never another cloud's store.
        """
        gcs_bucket = _resolved_env("ATLAN_GCP_BUCKET")
        s3_bucket = _resolved_env("ATLAN_S3_BUCKET")
        azure_account = _resolved_env("ATLAN_AZURE_STORAGE_ACCOUNT")
        if gcs_bucket:
            kind, build = "gcs", lambda: _gcs_store(gcs_bucket)
        elif s3_bucket:
            kind, build = "s3", lambda: _s3_store(s3_bucket)
        elif azure_account:
            kind, build = "azure", lambda: _azure_store(azure_account)
        else:
            return None
        try:
            store = build()
        except ModuleNotFoundError as exc:
            if exc.name != "obstore":
                raise
            # conformance: ignore[L004] the cause is fully named (obstore missing); its traceback adds nothing.
            logger.warning(
                "A %s object store is configured but obstore is not installed, so "
                "the config and file routes answer 503. Install "
                "atlan-application-sdk-api[obstore].",
                kind,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — logged at WARNING with the exception type only; its message can quote the store credential
            # Only the exception's type: obstore's message can quote the config
            # it rejected, and that config carries the store's credential.
            # conformance: ignore[L004] exc_info would log that message, and the credential with it.
            logger.warning(
                "The %s object store could not be built (%s), so the config and "
                "file routes answer 503.",
                kind,
                type(exc).__name__,
            )
            return None
        return None if store is None else cls(store, kind=kind)

    async def get_json(self, key: str) -> dict[str, Any] | None:
        """The object at ``key``, or ``None`` when it is missing.

        Any other failure (auth, network, throttling, a missing bucket or
        container) raises: the store could not answer, which is not a missing
        object.
        """
        import obstore  # noqa: PLC0415 — the obstore extra
        from obstore.exceptions import NotFoundError  # noqa: PLC0415

        try:
            result = await obstore.get_async(self._store, key)
            raw = await result.bytes_async()
        except (FileNotFoundError, NotFoundError):
            # Current obstore raises the built-in; older releases its own class.
            # S3, GCS and Azure also answer a missing bucket or container with
            # a 404, which obstore reports the same way. A listing under the key
            # tells them apart: it succeeds (empty) when only the object is
            # missing, and raises when the bucket or container is.
            await obstore.list_with_delimiter_async(self._store, prefix=key)
            return None
        return orjson.loads(bytes(raw))

    async def put_json(self, key: str, body: dict[str, Any]) -> None:
        import obstore  # noqa: PLC0415 — the obstore extra

        await obstore.put_async(
            self._store,
            key,
            orjson.dumps(body),
            attributes={"Content-Type": "application/json"},
        )

    async def put_file(self, key: str, local_path: str) -> None:
        import obstore  # noqa: PLC0415 — the obstore extra

        # obstore streams the file from disk, in parts when it is large.
        await obstore.put_async(self._store, _normalize_key(key), Path(local_path))


def _s3_store(bucket: str) -> ObjectStore:
    """S3 with the pod's ambient AWS identity, as the fleet's Dapr
    ``bindings.aws.s3`` components authenticate: their specs carry only a
    bucket and a region."""
    # As the worker's binding resolves it: obstore reads neither AWS_REGION nor
    # AWS_DEFAULT_REGION once given a config, and defaults to us-east-1.
    region = (
        _resolved_env("ATLAN_S3_REGION")
        or os.environ.get("AWS_REGION", "")
        or os.environ.get("AWS_DEFAULT_REGION", "")
    )
    config = {"aws_region": region} if region else None
    return _obstore_config.make_s3_store(bucket, config, label="host-s3")


def _gcs_store(bucket: str) -> ObjectStore | None:
    """GCS through its S3-compatible API, with the tenant's HMAC key pair.

    The host's manifest aliases the atlan-app chart's GCP-only ``GCP_*`` names
    as ``ATLAN_GCP_*``. The chart gives the app container no service-account
    key and no Workload Identity, only these HMAC keys, so a native GCSStore
    would have no credential. obstore sends no checksum headers unless asked,
    so GCS's rejection of boto3's default CRC32 does not arise.
    """
    access_key = _resolved_env("ATLAN_GCP_HMAC_ACCESS_KEY")
    secret = _resolved_env("ATLAN_GCP_HMAC_SECRET")
    if not (access_key and secret):
        logger.warning(
            "ATLAN_GCP_BUCKET is set without ATLAN_GCP_HMAC_ACCESS_KEY / "
            "ATLAN_GCP_HMAC_SECRET, so the config and file routes answer 503."
        )
        return None
    config = {
        "aws_endpoint": _GCS_ENDPOINT,
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret,
        # GCS rejects x-amz-tagging, as the worker's binding notes for every
        # non-AWS endpoint.
        "aws_disable_tagging": "true",
    }
    region = _resolved_env("ATLAN_GCP_REGION")
    if region:
        config["aws_region"] = region
    return _obstore_config.make_s3_store(bucket, config, label="host-gcs")


def _azure_store(account: str) -> ObjectStore | None:
    """The tenant's Blob container, as the chart's Dapr
    ``bindings.azure.blobstorage`` component reaches it.

    With ``ATLAN_AZURE_STORAGE_ACCESS_KEY`` it signs with that account key, and
    the shared constructor strips the pod's ambient ``AZURE_*`` while it builds
    the store, so the pod's own identity cannot outrank it. Without one, obstore
    uses the pod's ambient identity (the chart labels Azure pods for workload
    identity).
    """
    container = _resolved_env("ATLAN_AZURE_STORAGE_CONTAINER")
    if not container:
        logger.warning(
            "ATLAN_AZURE_STORAGE_ACCOUNT is set without "
            "ATLAN_AZURE_STORAGE_CONTAINER, so the config and file routes answer 503."
        )
        return None
    config = {"azure_storage_account_name": account}
    access_key = _resolved_env("ATLAN_AZURE_STORAGE_ACCESS_KEY")
    if access_key:
        config["azure_storage_account_key"] = access_key
    return _obstore_config.make_azure_store(container, config, label="host-azure")


def host_object_source(
    app_name: str, store: ObjectStoreOps | None
) -> ObjectRoutesSource:
    """The routes' store operations for one hosted app."""

    async def load_config(config_id: str, config_type: str) -> dict[str, Any] | None:
        if store is None:
            return None
        return await store.get_json(config_key(app_name, config_id, config_type))

    async def save_config(
        config_id: str, body: dict[str, Any], config_type: str
    ) -> bool:
        if store is None:
            return False
        await store.put_json(config_key(app_name, config_id, config_type), body)
        return True

    async def upload_file(key: str, local_path: str) -> None:
        if store is None:  # the route answers 503 before it gets here
            raise HTTPException(status_code=503, detail="Storage not configured")
        await store.put_file(key, local_path)

    return ObjectRoutesSource(
        configured=lambda: store is not None,
        load_config=load_config,
        save_config=save_config,
        upload_file=upload_file,
        upload_namespace=f"persistent-artifacts/apps/{app_name}/uploads",
    )
