"""Low-level obstore CRUD operations.

All functions accept an optional ``ObjectStore`` instance and a key string.
When ``store`` is omitted (or ``None``), the store is resolved from the
current :func:`~application_sdk.infrastructure.context.get_infrastructure`
context — the store is always transparent to callers.

By default keys are normalised via :func:`normalize_key` before being sent
to the store, so staging paths like ``./local/tmp/artifacts/...`` are
transparently converted to store keys like ``artifacts/...`` and leading/trailing
slashes are stripped.

Pass ``normalize=False`` to bypass normalisation and use the key exactly as
supplied (useful for callers that already hold a clean store key and want to
avoid the constant import overhead, or for tests that need exact key control).

Internal helpers (small payloads)
----------------------------------
* ``_put(key, data)``    — write bytes (sidecars, metadata, JSON configs)
* ``_get_bytes(key)``    — read bytes (sidecars, metadata, JSON configs)
Streaming API (large files)
----------------------------
* ``upload_file(key, local_path)``   — streaming upload with adaptive multipart
* ``download_file(key, local_path)`` — streaming download with optional hash
* ``objectstore.delete_prefix(prefix)``  →  ``delete_prefix(prefix)``

All calls that previously went through the implicit singleton store now
resolve from the infrastructure context automatically.  Pass ``store=my_store``
to target a specific store instead.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING

import obstore

if TYPE_CHECKING:
    from obstore.store import ObjectStore

# stdlib logger: cannot use get_logger here due to circular import
# (observability -> storage -> batch -> ops -> observability)
logger = logging.getLogger(__name__)


def normalize_key(key: str) -> str:
    """Normalise a local or object-store path into a clean object-store key.

    Accepts any of:

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

    from application_sdk.constants import TEMPORARY_PATH

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


def _resolve_store(store: "ObjectStore | None") -> "ObjectStore":
    """Return *store* if provided, otherwise resolve from the infrastructure context.

    Raises:
        RuntimeError: If *store* is ``None`` and no infrastructure context is set.
    """
    if store is not None:
        return store
    from application_sdk.infrastructure.context import get_infrastructure

    infra = get_infrastructure()
    if infra is None or infra.storage is None:
        raise RuntimeError(
            "No ObjectStore provided and no infrastructure storage is configured. "
            "Pass store= explicitly or call set_infrastructure() with a storage store."
        )
    return infra.storage


def _is_not_found(exc: Exception) -> bool:
    """Return True if the exception indicates a missing key."""
    msg = str(exc).lower()
    return (
        "not found" in msg
        or "no such file" in msg
        or "does not exist" in msg
        or "404" in msg
        or "key not found" in msg
    )


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
    local_path: "str | Path",
    store: "ObjectStore | None" = None,
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
        from application_sdk.storage.errors import StorageError

        raise StorageError(
            f"Failed to upload file to key '{key}'", key=key, cause=exc
        ) from exc

    digest = h.hexdigest()

    if not retain_local_copy:
        from application_sdk.constants import TEMPORARY_PATH

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
    local_path: "str | Path",
    store: "ObjectStore | None" = None,
    *,
    compute_hash: bool = False,
    min_chunk_size: int = 10 * 1024 * 1024,
    normalize: bool = True,
) -> "str | None":
    """Stream-download *key* from the store to a local file.

    Uses obstore's streaming GET so arbitrarily large files are written to
    disk without materialising the whole content in memory.

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

    try:
        result = await obstore.get_async(resolved, key)
    except Exception as exc:
        if _is_not_found(exc):
            from application_sdk.storage.errors import StorageNotFoundError

            raise StorageNotFoundError(
                f"Key not found in store: {key}", key=key
            ) from exc
        from application_sdk.storage.errors import StorageError

        raise StorageError(
            f"Failed to download key '{key}'", key=key, cause=exc
        ) from exc

    try:
        with path.open("wb") as fh:
            async for chunk in result.stream(min_chunk_size=min_chunk_size):
                raw = bytes(chunk)
                fh.write(raw)
                if h is not None:
                    h.update(raw)
    except Exception as exc:
        from application_sdk.storage.errors import StorageError

        raise StorageError(
            f"Failed to write downloaded file to '{local_path}'", key=key, cause=exc
        ) from exc

    return h.hexdigest() if h is not None else None


async def _get_bytes(
    key: str,
    store: "ObjectStore | None" = None,
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
        from application_sdk.storage.errors import StorageError

        raise StorageError(f"Failed to get key '{key}'", key=key, cause=exc) from exc


async def _put(
    key: str,
    data: bytes,
    store: "ObjectStore | None" = None,
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
        from application_sdk.storage.errors import StorageError

        raise StorageError(f"Failed to put key '{key}'", key=key, cause=exc) from exc


async def delete(
    key: str,
    store: "ObjectStore | None" = None,
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
        from application_sdk.storage.errors import StorageError

        raise StorageError(f"Failed to delete key '{key}'", key=key, cause=exc) from exc


async def exists(
    key: str,
    store: "ObjectStore | None" = None,
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
        from application_sdk.storage.errors import StorageError

        raise StorageError(
            f"Failed to check existence of key '{key}'", key=key, cause=exc
        ) from exc


delete_file = delete
