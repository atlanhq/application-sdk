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
* the host has no Dapr, so it uses :class:`S3ObjectStore`, configured from the
  chart-injected ``S3_BUCKET`` / ``S3_REGION`` with the pod's ambient AWS
  identity (the ``aws`` extra). Without ``S3_BUCKET`` the routes answer 503,
  exactly as the worker does without a store.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, cast
from uuid import uuid4

import orjson
from fastapi import FastAPI, File, Form, HTTPException
from fastapi import Path as PathParam
from fastapi import Query, Request, UploadFile
from fastapi.responses import JSONResponse

from application_sdk._logging import get_logger
from application_sdk._runtime.offload import run_in_thread
from application_sdk.handler.contracts import FileUploadResponse
from application_sdk.handler.routes import _wrap_response
from application_sdk.handler.service_errors import (
    InvalidConfigIdError,
    InvalidConfigTypeError,
    TempPathEscapeError,
)

logger = get_logger(__name__)

# Allowed characters for config_id and config_type path components. Prevents
# path traversal (no slashes, dots, or percent-encoding) when these values are
# interpolated into object-store keys.
CONFIG_KEY_PATTERN = r"^[a-zA-Z0-9_\-]{1,128}$"
_CONFIG_KEY_RE = re.compile(CONFIG_KEY_PATTERN)

# An upload prefix: one or more path segments of letters, digits, '_', '-' and
# '.', with no '..' segment and no leading, trailing or doubled '/'. It comes
# from the browser (Heracles forwards the form field unchanged), so it is
# untrusted input that becomes part of an object-store key.
UPLOAD_PREFIX_RE = re.compile(
    r"^(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*$"
)
_MAX_UPLOAD_PREFIX = 256

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
    """Resolve *path* and assert it lies within the system temp directory.

    Every temp path here comes from :func:`tempfile.mkstemp`; this makes the
    invariant explicit so SAST tools can confirm no user-supplied value can
    escape the temp directory.
    """
    tmp_root = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(path)
    if real != tmp_root and not real.startswith(tmp_root + os.sep):
        raise TempPathEscapeError()
    return real


@dataclass
class ObjectRoutesSource:
    """The store operations the routes need, for one app.

    Args:
        configured: Whether a store is configured. ``False`` makes every route
            answer 503.
        load_config: ``(config_id, config_type)`` → the stored config, or
            ``None`` when it is missing or unreadable.
        save_config: ``(config_id, body, config_type)`` → ``True`` once stored,
            ``False`` when no store is configured. Raises on a failed write.
        upload_file: ``(key, local_path)`` → stores the file at ``key``.
        upload_namespace: When set, every upload key is placed under it. The
            consolidated host sets the app's own
            ``persistent-artifacts/apps/<app>/uploads``: one identity writes to
            the shared bucket for every hosted app, so a request routed to one
            app must not be able to write under another's prefix.
    """

    configured: Callable[[], bool]
    load_config: Callable[[str, str], Awaitable[dict[str, Any] | None]]
    save_config: Callable[[str, dict[str, Any], str], Awaitable[bool]]
    upload_file: Callable[[str, str], Awaitable[None]]
    upload_namespace: str = ""


def register_object_routes(app: FastAPI, source: ObjectRoutesSource) -> None:
    """Register the config and file-upload routes on *app*."""

    @app.get("/workflows/v1/config/{config_id}")
    async def get_workflow_config(
        config_id: Annotated[str, PathParam(pattern=CONFIG_KEY_PATTERN)],
        type: Annotated[str, Query(pattern=CONFIG_KEY_PATTERN)] = "workflows",
    ) -> JSONResponse:
        """Fetch workflow config from object store."""
        config = await source.load_config(config_id, type)

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

    @app.post("/workflows/v1/file")
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
        if len(effective_prefix) > _MAX_UPLOAD_PREFIX or not UPLOAD_PREFIX_RE.match(
            effective_prefix
        ):
            raise HTTPException(
                status_code=400,
                detail="prefix must be relative path segments of letters, digits, "
                "'_', '-' or '.', with no '..'",
            )
        if source.upload_namespace:
            effective_prefix = f"{source.upload_namespace}/{effective_prefix}"
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


class S3ObjectStore:
    """One S3 bucket through a boto3 client: the host's store.

    Auth is ambient (node role / IRSA / env), exactly as the fleet's Dapr
    ``bindings.aws.s3`` components authenticate: their specs carry only a
    bucket and a region. No credential value is read here. boto3 calls run on
    the app's own ``run_in_thread`` pool, so S3 latency never blocks the host's
    event loop.
    """

    def __init__(self, bucket: str, region: str | None = None, client: Any = None):
        if client is None:
            import boto3  # noqa: PLC0415 — the aws extra; loaded only when S3 is configured

            client = boto3.client("s3", region_name=region or None)
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_env(cls) -> S3ObjectStore | None:
        """From ``S3_BUCKET`` / ``S3_REGION``; ``None`` when no bucket is set."""
        bucket = os.environ.get("S3_BUCKET", "").strip()
        if not bucket:
            return None
        try:
            return cls(bucket, os.environ.get("S3_REGION", "").strip() or None)
        except ModuleNotFoundError:
            logger.warning(
                "S3_BUCKET is set but boto3 is not installed, so the config and "
                "file routes answer 503. Install atlan-application-sdk-api[aws].",
                exc_info=True,
            )
            return None

    async def get_json(self, key: str) -> dict[str, Any] | None:
        """The object at ``key``, or ``None`` when it is missing or unreadable."""

        def _get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()

        try:
            return orjson.loads(await run_in_thread(_get))
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code not in ("NoSuchKey", "404"):
                # A missing object is the ordinary 404; anything else (IAM,
                # network, throttling) is an operational problem worth a line.
                logger.warning(
                    "Object-store config load failed for key=%s", key, exc_info=True
                )
            return None

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


def host_object_source(
    app_name: str, store: S3ObjectStore | None
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
