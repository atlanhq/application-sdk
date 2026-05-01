"""Low-level obstore CRUD operations.

All functions accept an optional ``ObjectStore`` instance and a key string.
When ``store`` is omitted (or ``None``), the store is resolved from the
current :func:`~application_sdk.infrastructure.context.get_infrastructure`
context — mirroring the v2 behaviour where the store was always transparent.

By default keys are normalised via :func:`normalize_key` before being sent
to the store, which mirrors the automatic normalisation that the deprecated
``ObjectStore.as_store_key()`` performed in v2 — so staging paths like
``./local/tmp/artifacts/...`` are transparently converted to store keys like
``artifacts/...`` and leading/trailing slashes are stripped.

Pass ``normalize=False`` to bypass normalisation and use the key exactly as
supplied (useful for callers that already hold a clean store key and want to
avoid the constant import overhead, or for tests that need exact key control).

Public helpers (small payloads)
--------------------------------
* ``put_json(key, obj)`` — serialise to JSON and write (configs, metadata)
* ``_get_bytes(key)``    — read bytes (sidecars, metadata, JSON configs)

Internal helpers
----------------
* ``_put(key, data)``    — write raw bytes (use ``put_json`` for JSON)

Streaming API (large files)
----------------------------
* ``upload_file(key, local_path)``   — streaming upload with adaptive multipart
* ``download_file(key, local_path)`` — streaming download with optional hash

All calls that previously went through the implicit singleton store now
resolve from the infrastructure context automatically.  Pass ``store=my_store``
to target a specific store instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import obstore
import orjson

# obstore-rs surfaces a typed exception hierarchy via obstore.exceptions; we
# detect it once at import time so callers don't pay the import cost on every
# error path.  Falls back to substring matching for older obstore versions
# that lack typed exceptions.
try:  # pragma: no cover — defensive import
    from obstore.exceptions import BaseError as _ObstoreBaseError
    from obstore.exceptions import NotFoundError as _ObstoreNotFoundError
except ImportError:  # pragma: no cover
    _ObstoreBaseError = None  # type: ignore[assignment,misc]
    _ObstoreNotFoundError = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from typing import Any

    from obstore.store import ObjectStore

    JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

# stdlib logger: cannot use get_logger here due to circular import
# (observability -> storage -> batch -> ops -> observability)
logger = logging.getLogger(__name__)


def normalize_key(key: str) -> str:
    """Normalise a local or object-store path into a clean object-store key.

    Mirrors the behaviour of the deprecated ``ObjectStore.as_store_key()``
    to provide a smooth v2 → v3 migration path.  Accepts any of:

    * Local SDK staging paths (``./local/tmp/artifacts/...``) — the
      ``TEMPORARY_PATH`` prefix is stripped so callers get
      ``artifacts/...`` regardless of where the temp root is mounted.
    * Absolute paths (``/data/output.parquet`` → ``data/output.parquet``).
    * Already-relative store keys (``artifacts/foo/bar.jsonl``) — returned
      unchanged (normalisation is a no-op for clean keys).

    Backslashes are converted to forward slashes and leading/trailing slashes
    are stripped so the result is always a clean relative key.

    Args:
        key: Raw key, local path, or staging path to normalise.

    Returns:
        Normalised object-store key, or empty string for empty input.
    """
    if not key:
        return ""

    from application_sdk.constants import TEMPORARY_PATH  # noqa: PLC0415

    abs_path = os.path.abspath(key)
    abs_temp_path = os.path.abspath(TEMPORARY_PATH)
    try:
        common_path = os.path.commonpath([abs_path, abs_temp_path])
        if common_path == abs_temp_path:
            # Path is inside the staging area — strip the staging prefix.
            normalized = os.path.relpath(abs_path, abs_temp_path).replace(os.sep, "/")
        else:
            normalized = key.strip("/")
    except ValueError:
        # os.path.commonpath raises on mixed Windows drives; fall back to simple strip.
        normalized = key.strip("/")

    normalized = normalized.replace("\\", "/").replace(os.sep, "/").strip("/")
    # os.path.relpath resolves the staging root itself to "."; treat as store root.
    return "" if normalized == "." else normalized


def _normalize_listing_prefix(prefix: str, normalize: bool) -> str:
    """Return *prefix* normalised for a listing call.

    Applies :func:`normalize_key` when *normalize* is ``True``, then ensures
    the result ends with ``"/"`` so prefix matching never bleeds into sibling
    directories.
    """
    if normalize and prefix:
        prefix = normalize_key(prefix)
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"
    return prefix


def _resolve_store(store: ObjectStore | None) -> ObjectStore:
    """Return *store* if provided, otherwise resolve from the infrastructure context.

    Raises:
        RuntimeError: If *store* is ``None`` and no infrastructure context is set.
    """
    if store is not None:
        return store
    from application_sdk.infrastructure.context import (  # noqa: PLC0415
        get_infrastructure,
    )

    infra = get_infrastructure()
    if infra is None or infra.storage is None:
        raise RuntimeError(
            "No ObjectStore provided and no infrastructure storage is configured. "
            "Pass store= explicitly or call set_infrastructure() with a storage store."
        )
    return infra.storage


def _is_not_found(exc: BaseException) -> bool:
    """Return True if the exception indicates a missing key.

    Recognises:

    * Built-in :class:`FileNotFoundError` — what current obstore (>=0.9) raises
      for missing keys after the deprecation of
      ``obstore.exceptions.NotFoundError``.
    * :class:`obstore.exceptions.NotFoundError` — still emitted by older
      obstore versions and present in the type stubs for forward-compat.
    * Substring fallback (``"not found"``, ``"404"``, …) for generic obstore
      errors that surface only as ``GenericError`` with the underlying HTTP
      status in the message.

    Class-based detection runs first so we don't misclassify a generic
    ``GenericError("HTTP 503: 404 not in title")`` style flake.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    if _ObstoreNotFoundError is not None and isinstance(exc, _ObstoreNotFoundError):
        return True
    msg = str(exc).lower()
    return (
        "not found" in msg
        or "no such file" in msg
        or "does not exist" in msg
        or "404" in msg
        or "key not found" in msg
    )


def _exc_class_name(exc: BaseException) -> str:
    """Return a stable short class name for structured-log error_class fields."""
    return type(exc).__name__


def _throughput_mbps(size_bytes: int, elapsed_ms: float) -> float | None:
    """Return MiB/s throughput, or ``None`` when unknown / instantaneous."""
    if elapsed_ms <= 0 or size_bytes <= 0:
        return None
    return round((size_bytes / (1024 * 1024)) / (elapsed_ms / 1000.0), 3)


def _log_storage_event(
    level: int,
    op: str,
    store_path: str,
    *,
    outcome: str,
    elapsed_ms: float | None = None,
    size_bytes: int | None = None,
    error_class: str | None = None,
) -> None:
    """Emit a single structured per-attempt storage event.

    Fields are placed on ``extra`` so structured-log backends and pytest's
    caplog see them as record attributes; the human-readable message stays
    short for unstructured tail / grep workflows.
    """
    extra: dict[str, object] = {
        "storage_op": op,
        "store_path": store_path,
        "outcome": outcome,
    }
    if elapsed_ms is not None:
        extra["elapsed_ms"] = round(elapsed_ms, 3)
    if size_bytes is not None:
        extra["size_bytes"] = size_bytes
        if elapsed_ms is not None:
            tput = _throughput_mbps(size_bytes, elapsed_ms)
            if tput is not None:
                extra["throughput_mibps"] = tput
    if error_class is not None:
        extra["error_class"] = error_class
    msg = f"storage.{op} {outcome} path={store_path}"
    logger.log(level, msg, extra=extra)  # lgtm[py/clear-text-logging-sensitive-data]


async def _list_items(
    store: ObjectStore,
    prefix: str | None,
    *,
    include_markers: bool = False,
) -> list[tuple[str, int]]:
    """Collect listing results under *prefix*, optionally filtering GCS directory markers.

    Makes a single listing operation (``obstore.list`` returns a native async
    ``ListStream`` that pages internally — no thread wrapping needed).  When *include_markers* is
    ``False``, two additional in-memory passes are applied: one to build the set of
    ancestor path segments, and one to filter out zero-byte objects whose path is one
    of those ancestors (the structural signature of a GCS-console "folder" marker).

    A zero-byte object is excluded when its path is a strict path-prefix of at least
    one other listed key — i.e. it acts as a parent directory for real files.

    Args:
        store: An obstore-compatible store instance.
        prefix: Key prefix, or ``None`` to list everything.
        include_markers: When ``True``, skip the directory-marker filter and return
            every object including zero-byte markers.  Use this when the caller must
            operate on *all* objects (e.g. ``delete_prefix``) so that no orphan
            objects are left behind on any store backend.

    Returns:
        ``(path, size)`` tuples in listing order.  Directory markers are excluded
        unless *include_markers* is ``True``.
    """
    all_items: list[tuple[str, int]] = []
    async for batch in obstore.list(store, prefix=prefix):  # native async ListStream
        for item in batch:
            all_items.append((str(item["path"]), int(item["size"])))

    if include_markers:
        return all_items

    parent_dirs: set[str] = set()
    for path, _ in all_items:
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent_dirs.add("/".join(parts[:i]))

    return [
        (path, size)
        for path, size in all_items
        if not (size == 0 and path in parent_dirs)
    ]


def _compute_part_size(file_size: int, chunk_size: int) -> int:
    """Compute effective upload part size to stay under S3's 10,000-part limit.

    Args:
        file_size: Total file size in bytes.
        chunk_size: Desired chunk size in bytes.

    Returns:
        Effective part size — at least *chunk_size* but never so small that
        more than 9,900 parts would be needed (safety margin below 10,000).
    """
    return max(chunk_size, math.ceil(file_size / 9900))


async def upload_file(
    key: str,
    local_path: str | Path,
    store: ObjectStore | None = None,
    *,
    chunk_size: int = 8 * 1024 * 1024,
    normalize: bool = True,
    retain_local_copy: bool = True,
) -> str:
    """Stream-upload a local file to *key* in the store.

    Uses obstore's multipart writer so arbitrarily large files are uploaded
    without materialising the whole content in memory.  The part size is
    adapted automatically to stay under S3's 10,000-part limit.

    A single pass over the file simultaneously feeds each chunk to the
    SHA-256 hasher and the store writer.

    Args:
        key: Destination object key.  Normalised by default.
        local_path: Path to the local file to upload.
        store: Target store, or ``None`` to use the infrastructure store.
        chunk_size: Desired chunk / part size in bytes (default 8 MiB).
            Increased automatically if the file is large enough to exceed
            the 9,900-part safety limit.
        normalize: When ``True`` (default), normalise *key* before use.
        retain_local_copy: When ``True`` (default), keep the local file after
            upload.  When ``False``, delete the local file after a successful
            upload.

    Returns:
        Hex-encoded SHA-256 digest of the uploaded file.

    Raises:
        StorageError: If the upload fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    file_size = path.stat().st_size
    effective_chunk = _compute_part_size(file_size, chunk_size)

    h = hashlib.sha256()
    started = time.monotonic()
    try:
        async with obstore.open_writer_async(
            resolved, key, buffer_size=effective_chunk
        ) as writer:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(effective_chunk)
                    if not chunk:
                        break
                    h.update(chunk)
                    await writer.write(chunk)
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        _log_storage_event(
            logging.WARNING,
            "upload",
            key,
            outcome="failure",
            elapsed_ms=elapsed_ms,
            size_bytes=file_size,
            error_class=_exc_class_name(exc),
        )
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(
            f"Failed to upload file to key '{key}'", key=key, cause=exc
        ) from exc

    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log_storage_event(
        logging.INFO,
        "upload",
        key,
        outcome="success",
        elapsed_ms=elapsed_ms,
        size_bytes=file_size,
    )
    digest = h.hexdigest()

    if not retain_local_copy:
        from application_sdk.constants import TEMPORARY_PATH  # noqa: PLC0415

        resolved_path = path.resolve()
        staging_root = Path(TEMPORARY_PATH).resolve()
        # Only delete files within the staging directory to prevent path traversal
        if resolved_path.is_relative_to(staging_root):
            try:
                resolved_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug(
                    "Failed to delete local file (cleanup): %s", type(exc).__name__
                )

    return digest


async def download_file(
    key: str,
    local_path: str | Path,
    store: ObjectStore | None = None,
    *,
    compute_hash: bool = False,
    min_chunk_size: int = 10 * 1024 * 1024,
    normalize: bool = True,
) -> str | None:
    """Stream-download *key* from the store to a local file.

    Uses obstore's streaming GET so arbitrarily large files are written to
    disk without materialising the whole content in memory.

    Transient failures (network errors, mid-body stream drops, HTTP 5xx) are
    retried automatically by the Rust layer (obstore / object_store crate) using
    the ``ClientConfig`` timeout and ``RetryConfig`` values configured in
    ``_obstore_config.py`` — notably ``ATLAN_OBSTORE_TIMEOUT=30m`` (default)
    which prevents the 30-second body-read cut-off that caused the
    Autodesk/Mindbody incidents.  There is no additional Python-level retry loop
    here to avoid multiplying wait time without changing the outcome.

    Args:
        key: Source object key.  Normalised by default.
        local_path: Destination path (file will be created or overwritten).
            Parent directories are created automatically.
        store: Source store, or ``None`` to use the infrastructure store.
        compute_hash: When ``True``, compute and return the SHA-256 digest
            while streaming.  When ``False`` (default), returns ``None``.
        min_chunk_size: Minimum chunk size hint passed to the stream iterator
            (default 10 MiB).
        normalize: When ``True`` (default), normalise *key* before use.

    Returns:
        Hex-encoded SHA-256 digest if *compute_hash* is ``True``, else ``None``.

    Raises:
        StorageNotFoundError: If *key* does not exist in the store.
        StorageError: If the download or write fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256() if compute_hash else None
    started = time.monotonic()

    try:
        result = await obstore.get_async(resolved, key)
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if _is_not_found(exc):
            _log_storage_event(
                logging.WARNING,
                "download",
                key,
                outcome="failure",
                elapsed_ms=elapsed_ms,
                error_class="StorageNotFoundError",
            )
            from application_sdk.storage.errors import (  # noqa: PLC0415
                StorageNotFoundError,
            )

            raise StorageNotFoundError(
                f"Key not found in store: {key}", key=key
            ) from exc
        _log_storage_event(
            logging.WARNING,
            "download",
            key,
            outcome="failure",
            elapsed_ms=elapsed_ms,
            error_class=_exc_class_name(exc),
        )
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(
            f"Failed to download key '{key}'", key=key, cause=exc
        ) from exc

    bytes_written = 0
    try:
        with path.open("wb") as fh:
            async for chunk in result.stream(min_chunk_size=min_chunk_size):
                raw = bytes(chunk)
                fh.write(raw)
                bytes_written += len(raw)
                if h is not None:
                    h.update(raw)
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        _log_storage_event(
            logging.WARNING,
            "download",
            key,
            outcome="failure",
            elapsed_ms=elapsed_ms,
            size_bytes=bytes_written,
            error_class=_exc_class_name(exc),
        )
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(
            f"Failed to write downloaded file to '{local_path}'", key=key, cause=exc
        ) from exc

    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log_storage_event(
        logging.INFO,
        "download",
        key,
        outcome="success",
        elapsed_ms=elapsed_ms,
        size_bytes=bytes_written,
    )
    return h.hexdigest() if h is not None else None


async def get_file_size(
    key: str,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> int | None:
    """Return the byte size of *key* via a HEAD request, or ``None`` if not found.

    Uses a lightweight metadata-only request; the object body is never
    transferred.  Raises :class:`~application_sdk.storage.errors.StorageError`
    for non-404 errors (permission denied, I/O error, etc.).

    Args:
        key: Object key / path.  Normalised by default.
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.

    Returns:
        File size in bytes, or ``None`` if the key does not exist.

    Raises:
        StorageError: For non-404 errors.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        meta = await obstore.head_async(resolved, key)
        return int(meta["size"])
    except Exception as exc:
        if _is_not_found(exc):
            return None
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(f"Failed to head key '{key}'", key=key, cause=exc) from exc


async def download_file_chunked(
    key: str,
    local_path: str | Path,
    store: ObjectStore | None = None,
    *,
    chunk_size_bytes: int = 16 * 1024 * 1024,
    max_concurrent_chunks: int = 4,
    compute_hash: bool = True,
    normalize: bool = True,
) -> str | None:
    """Download *key* using parallel range GETs, writing chunks at fixed offsets.

    For files larger than *chunk_size_bytes*, issues multiple independent
    ``get_range_async`` requests (up to *max_concurrent_chunks* in flight at
    once) and writes each chunk to the correct file offset via ``os.pwrite``.
    Each chunk gets its own obstore retry budget, so a mid-stream stall only
    retries the affected chunk — not the entire file.

    Falls through to :func:`download_file` (single streaming GET) when the
    remote object is smaller than *chunk_size_bytes*.

    Args:
        key: Source object key.  Normalised by default.
        local_path: Destination path (created / overwritten).
        store: Source store, or ``None`` to use the infrastructure store.
        chunk_size_bytes: Size of each range-GET chunk (default 16 MiB).
        max_concurrent_chunks: Maximum number of in-flight chunk requests
            (default 4).
        compute_hash: When ``True`` (default), compute and return a SHA-256
            digest over the completed file.
        normalize: When ``True`` (default), normalise *key* before use.

    Returns:
        Hex-encoded SHA-256 digest if *compute_hash* is ``True``, else ``None``.

    Raises:
        StorageNotFoundError: If *key* does not exist.
        StorageError: If a chunk download or the disk write fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """

    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # HEAD to get exact size before allocating; also serves as the existence check.
    try:
        meta = await obstore.head_async(resolved, key)
        file_size = int(meta["size"])
    except Exception as exc:
        if _is_not_found(exc):
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                StorageNotFoundError,
            )

            raise StorageNotFoundError(
                f"Key not found in store: {key}", key=key
            ) from exc
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(f"Failed to head key '{key}'", key=key, cause=exc) from exc

    # Small files: delegate to the single-stream path so they still use the
    # streaming GET (avoids allocating the whole body in memory via get_range_async).
    if file_size <= chunk_size_bytes:
        return await download_file(
            key, local_path, resolved, compute_hash=compute_hash, normalize=False
        )

    # Pre-allocate the file at the target size so pwrite can address any offset.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        os.ftruncate(fd, file_size)
    except Exception:
        os.close(fd)
        raise

    sem = asyncio.Semaphore(max_concurrent_chunks)

    async def _fetch_chunk(offset: int) -> None:
        length = min(chunk_size_bytes, file_size - offset)
        async with sem:
            try:
                raw = bytes(
                    await obstore.get_range_async(
                        resolved, key, start=offset, length=length
                    )
                )
                os.pwrite(fd, raw, offset)
            except Exception as chunk_exc:
                raise chunk_exc

    try:
        await asyncio.gather(
            *(_fetch_chunk(off) for off in range(0, file_size, chunk_size_bytes))
        )
    except Exception as exc:
        os.close(fd)
        path.unlink(missing_ok=True)
        if _is_not_found(exc):
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                StorageNotFoundError,
            )

            raise StorageNotFoundError(
                f"Key not found during chunked download: {key}", key=key
            ) from exc
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(
            f"Chunked download failed for '{key}'", key=key, cause=exc
        ) from exc

    os.close(fd)

    if not compute_hash:
        return None

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _get_bytes(
    key: str,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> bytes | None:
    """Fetch the bytes stored at *key*, or ``None`` if the key does not exist.

    **Internal use only** — intended for small payloads (sidecars, metadata,
    JSON configs).  For large files use :func:`download_file` instead.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.
            Pass ``False`` to use *key* exactly as supplied.

    Returns:
        Raw bytes, or ``None`` if the key was not found.

    Raises:
        StorageError: For non-404 errors (permission denied, I/O error, etc.).
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        result = await obstore.get_async(resolved, key)
        # GetResult.bytes() is the sync accessor; bytes(result) iterates
        # over Rust Bytes chunks and yields bytes objects, not ints.
        raw = result.bytes()
        return bytes(raw)
    except Exception as exc:
        if _is_not_found(exc):
            return None
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(f"Failed to get key '{key}'", key=key, cause=exc) from exc


async def _put(
    key: str,
    data: bytes,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> None:
    """Write *data* to *key* in the store (creates or overwrites).

    **Internal use only** — intended for small payloads (sidecars, metadata,
    JSON configs).  For large files use :func:`upload_file` instead.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        data: Raw bytes to write.
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.
            Pass ``False`` to use *key* exactly as supplied.

    Raises:
        StorageError: If the write fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        await obstore.put_async(resolved, key, data)
    except Exception as exc:
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(f"Failed to put key '{key}'", key=key, cause=exc) from exc


async def put_json(
    key: str,
    obj: JsonValue,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> None:
    """Serialise *obj* to JSON and write to *key*.

    Convenience wrapper around :func:`_put` for small JSON payloads such as
    workflow configs and sidecar metadata.  For large files use
    :func:`upload_file` instead.

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        obj: A JSON-serialisable value (dict, list, str, int, float, bool, or None).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.

    Raises:
        StorageError: If the write fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    await _put(key, orjson.dumps(obj), store, normalize=normalize)


async def delete(
    key: str,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> bool:
    """Delete the object at *key*.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.
            Pass ``False`` to use *key* exactly as supplied.

    Returns:
        ``True`` if deleted, ``False`` if the key did not exist.

    Raises:
        StorageError: For non-404 errors.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        await obstore.delete_async(resolved, key)
        return True
    except Exception as exc:
        if _is_not_found(exc):
            return False
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(f"Failed to delete key '{key}'", key=key, cause=exc) from exc


async def exists(
    key: str,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> bool:
    """Return ``True`` if *key* exists in the store.

    Uses a HEAD request (metadata only) — the object content is never
    downloaded, so this is safe to call on arbitrarily large objects.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.

    Returns:
        ``True`` if the object exists, ``False`` otherwise.

    Raises:
        StorageError: For non-404 errors (permission denied, I/O error, etc.).
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        await obstore.head_async(resolved, key)
        return True
    except Exception as exc:
        if _is_not_found(exc):
            return False
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(
            f"Failed to check existence of key '{key}'", key=key, cause=exc
        ) from exc
