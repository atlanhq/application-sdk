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
  chart-injected ``ATLAN_S3_BUCKET`` / ``ATLAN_S3_REGION`` with the pod's ambient AWS
  identity (the ``aws`` extra), or on GCP from the chart's ``GCP_BUCKET`` through
  GCS's S3-compatible API. Without a bucket the routes answer 503, exactly as the
  worker does without a store.

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
from pathlib import PurePosixPath
from typing import Annotated, Any, Protocol, cast
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
        if source.upload_namespace:
            # The consolidated host: one AWS identity and one bucket serve every
            # hosted app, so a request routed to one app must not choose a key
            # outside that app's namespace. A worker serves only its own app and
            # keeps accepting the prefix as given, as it always has.
            if not valid_upload_prefix(effective_prefix):
                raise HTTPException(
                    status_code=400,
                    detail="prefix must be relative path segments of letters, "
                    "digits, '_', '-' or '.', with no '.' or '..' segment",
                )
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


class S3Client(Protocol):
    """The boto3 S3 client calls :class:`S3ObjectStore` makes.

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


class S3ObjectStore:
    """One S3 bucket through a boto3 client: the host's store.

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
        ``GCP_BUCKET`` (:meth:`_gcs_from_env`); ``None`` when no bucket is set."""
        gcs_bucket = os.environ.get("GCP_BUCKET", "").strip()
        if gcs_bucket:
            # First: the chart injects no S3_* on GCP, so the host's ATLAN_S3_*
            # aliases are literal "$(S3_BUCKET)" text there.
            return cls._gcs_from_env(gcs_bucket)
        bucket = os.environ.get("ATLAN_S3_BUCKET", "").strip()
        # Azure Blob tenants get no S3_* either: the literal alias is no bucket
        # (Blob is not served yet), not a bucket name.
        if not bucket or bucket.startswith("$("):
            return None
        try:
            return cls(bucket, os.environ.get("ATLAN_S3_REGION", "").strip() or None)
        except ModuleNotFoundError:
            logger.warning(
                "ATLAN_S3_BUCKET is set but boto3 is not installed, so the config and "
                "file routes answer 503. Install atlan-application-sdk-api[aws].",
                exc_info=True,
            )
            return None

    @classmethod
    def _gcs_from_env(cls, bucket: str) -> S3ObjectStore | None:
        """GCS through its S3-compatible API, with the chart's HMAC key pair.

        The atlan-app chart's own names, set on GCP only: an ``ATLAN_`` alias
        in the host's manifest would be literal ``$(GCP_BUCKET)`` elsewhere.
        """
        access_key = os.environ.get("GCP_HMAC_ACCESS_KEY", "").strip()
        secret = os.environ.get("GCP_HMAC_SECRET", "").strip()
        if not (access_key and secret):
            logger.warning(
                "GCP_BUCKET is set without GCP_HMAC_ACCESS_KEY / GCP_HMAC_SECRET, "
                "so the config and file routes answer 503."
            )
            return None
        try:
            import boto3  # noqa: PLC0415 — the aws extra; loaded only when GCS is configured
            from botocore.config import Config  # noqa: PLC0415 — ships with boto3
        except ModuleNotFoundError:
            logger.warning(
                "GCP_BUCKET is set but boto3 is not installed, so the config and "
                "file routes answer 503. Install atlan-application-sdk-api[aws].",
                exc_info=True,
            )
            return None
        client = boto3.client(
            "s3",
            endpoint_url=_GCS_ENDPOINT,
            region_name=os.environ.get("GCP_REGION", "").strip() or None,
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
