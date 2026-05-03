"""Batch and prefix storage operations.

Higher-level operations that work on groups of objects (prefixes, directories).
These build on the single-object primitives in :mod:`storage.ops`.

Batch upload/download
---------------------
* ``upload_prefix(local_dir, prefix)``     — upload a directory tree
* ``download_prefix(prefix, local_dir)``   — download a prefix tree
* ``upload_file_from_bytes(key, content)``  — upload bytes via temp file
* ``delete_prefix(prefix)``                — delete all objects under prefix
* ``list_keys(prefix)``                    — list object keys under prefix

Param order convention: source first, destination second.
``upload_prefix(local_dir, prefix)`` and ``download_prefix(prefix, local_dir)``
both follow this — the data source is always the first positional argument.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import obstore

from application_sdk.storage.ops import (
    _is_not_found,
    _list_items,
    _normalize_listing_prefix,
    _resolve_store,
    download_file,
    normalize_key,
    upload_file,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore


async def list_keys(
    prefix: str = "",
    store: ObjectStore | None = None,
    *,
    suffix: str = "",
    normalize: bool = True,
    include_markers: bool = False,
) -> list[str]:
    """List all object keys under *prefix*.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`~storage.ops._resolve_store`).

    Args:
        prefix: Key prefix to filter by.  Empty string lists all keys.
            Normalised by default (see :func:`normalize_key`).  A trailing
            ``/`` is preserved (or added) after normalisation so that prefix
            matching never bleeds into sibling directories
            (e.g. ``"artifacts"`` won't match ``"artifacts_backup/"``).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        suffix: Optional file extension or suffix filter.  When set, only
            keys whose path ends with this string are returned
            (e.g. ``".parquet"``).  The match is case-insensitive.
        normalize: When ``True`` (default), normalise *prefix* before use.
            Pass ``False`` to use *prefix* exactly as supplied.
        include_markers: When ``False`` (default), zero-byte objects that act
            as GCS-style directory markers (i.e. they have at least one child
            key under them) are excluded from results.  Pass ``True`` to
            bypass this filter and receive every object including markers —
            useful when the caller needs to operate on all objects regardless
            of size (e.g. ``delete_prefix``).

    Returns:
        Sorted list of matching object keys.  By default, zero-byte objects
        that act as GCS-style directory markers are excluded; zero-byte files
        with no children are returned normally.  Marker detection is
        single-pass: a zero-byte object is only identified as a marker if its
        children appear in the same listing call (i.e. they share the
        requested *prefix*).

    Raises:
        StorageError: If the listing fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    prefix = _normalize_listing_prefix(prefix, normalize)

    try:
        items = await _list_items(
            resolved, prefix or None, include_markers=include_markers
        )
        lsuffix = suffix.lower() if suffix else ""
        return sorted(
            path for path, _ in items if not lsuffix or path.lower().endswith(lsuffix)
        )
    except Exception as exc:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(
            f"Failed to list keys with prefix '{prefix}'", cause=exc
        ) from exc


async def delete_prefix(
    prefix: str,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> int:
    """Delete all objects whose key starts with *prefix*.

    Uses the store's native bulk-delete API where available (S3 batches up to
    1 000 keys per request; Azure up to 256; GCS issues 10 parallel individual
    DELETE requests).  A not-found error on GCS or Azure after a fresh listing
    indicates concurrent modification of the same prefix and is surfaced as a
    ``StorageError`` rather than silently retried — such a failure almost
    certainly signals an unexpected interaction between two apps sharing the
    same prefix, which is a bug worth making explicit.

    Args:
        prefix: Key prefix — all objects under this prefix are deleted.
            Normalised by default (see :func:`normalize_key`).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *prefix* before use.

    Returns:
        Number of objects deleted.

    Raises:
        StorageError: If the listing or any deletion fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    prefix = _normalize_listing_prefix(prefix, normalize)

    # include_markers=True so intermediate zero-byte "folder" objects within
    # the requested prefix (e.g. "artifacts/run/sub" when deleting "artifacts/")
    # are deleted alongside real files.  Note: the marker *at* the prefix root
    # (e.g. "artifacts/run" when prefix = "artifacts/run/") sits outside the
    # prefix-filtered listing due to trailing-slash normalisation and is handled
    # separately below via a best-effort delete of the bare root key.
    try:
        items = await _list_items(resolved, prefix or None, include_markers=True)
    except Exception as exc:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(
            f"Failed to list keys with prefix '{prefix}'", cause=exc
        ) from exc

    paths = [path for path, _ in items]

    # Also delete the directory marker at the prefix root itself (e.g. the GCS
    # object "artifacts/run" when prefix = "artifacts/run/").  obstore strips
    # trailing slashes from all keys, so the marker for the requested directory
    # never starts with the slash-terminated prefix and is not returned by the
    # listing above.  Probe with HEAD first (rather than DELETE-and-swallow)
    # because some backends (MemoryStore, S3) silently succeed on deleting a
    # non-existent key, which would inflate the count.
    root_marker = prefix.rstrip("/")
    if root_marker and root_marker not in paths:
        try:
            await obstore.head_async(resolved, root_marker)
            paths.append(root_marker)
        except Exception as exc:
            if not _is_not_found(exc):
                from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                    StorageError,
                )

                raise StorageError(
                    f"Failed to check root marker '{root_marker}'", cause=exc
                ) from exc
            # not-found → no marker exists, nothing to add

    if not paths:
        return 0

    try:
        await obstore.delete_async(resolved, paths)
    except Exception as exc:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(
            f"Failed to delete {len(paths)} objects with prefix '{prefix}'", cause=exc
        ) from exc

    return len(paths)


async def download_prefix(
    prefix: str,
    local_dir: str | Path,
    store: ObjectStore | None = None,
    *,
    suffix: str = "",
    normalize: bool = True,
    max_concurrency: int = 4,
) -> list[str]:
    """Download all objects under *prefix* to a local directory.

    Each key's full store path is preserved under *local_dir*
    (e.g. key ``artifacts/run/file.json`` → ``local_dir/artifacts/run/file.json``).
    Downloads run concurrently (up to *max_concurrency* at a time).

    Args:
        prefix: Object key prefix to download.
        local_dir: Local directory to write files into.
        store: Source store, or ``None`` to use the infrastructure store.
        suffix: Optional extension filter (e.g. ``".parquet"``).
        normalize: When ``True`` (default), normalise *prefix* before use.
        max_concurrency: Maximum parallel downloads (default 4).

    Returns:
        List of local file paths that were downloaded.

    Raises:
        StorageError: If listing or downloading fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    import asyncio  # noqa: PLC0415 — stdlib asyncio; lazy use only

    keys = await list_keys(prefix, store, suffix=suffix, normalize=normalize)
    local = Path(local_dir)
    # S3 keys use forward slashes; convert to OS-native path for local filesystem
    destinations = [str(local / Path(*PurePosixPath(key).parts)) for key in keys]

    sem = asyncio.Semaphore(max_concurrency)

    async def _download_one(key: str, dest: str) -> None:
        async with sem:
            await download_file(key, dest, store, normalize=False)

    await asyncio.gather(*[_download_one(k, d) for k, d in zip(keys, destinations)])
    return destinations


async def upload_prefix(
    local_dir: str | Path,
    prefix: str,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
    retain_local_copy: bool = True,
    max_concurrency: int = 4,
) -> list[str]:
    """Upload all files under *local_dir* to the store under *prefix*.

    Each file's relative path is preserved under *prefix*.
    Symlinks are skipped to prevent path-traversal.

    Note:
        Param order is ``(local_dir, prefix)`` — source first, destination second.
        This is the inverse of :func:`download_prefix` ``(prefix, local_dir)`` which
        also follows source-first convention.

    Args:
        local_dir: Local directory to upload from.
        prefix: Destination key prefix in the store.
        store: Target store, or ``None`` to use the infrastructure store.
        normalize: When ``True`` (default), normalise *prefix* before use.
        retain_local_copy: When ``True`` (default), keep local files.
        max_concurrency: Maximum parallel uploads (default 4).

    Returns:
        List of uploaded object keys.
    """
    import asyncio  # noqa: PLC0415 — stdlib asyncio; lazy use only

    local = Path(local_dir)
    if normalize and prefix:
        prefix = normalize_key(prefix)

    files: list[tuple[str, Path]] = []
    for root, _dirs, filenames in os.walk(local, followlinks=False):
        for fname in filenames:
            file_path = Path(root) / fname
            if file_path.is_symlink():
                continue
            rel = file_path.relative_to(local)
            # Use PurePosixPath to ensure forward slashes in S3 keys (Windows uses backslash)
            rel_posix = PurePosixPath(*rel.parts)
            key = f"{prefix}/{rel_posix}" if prefix else str(rel_posix)
            files.append((key, file_path))

    sem = asyncio.Semaphore(max_concurrency)
    uploaded: list[str] = []

    async def _upload_one(key: str, path: Path) -> None:
        async with sem:
            await upload_file(
                key, path, store, normalize=False, retain_local_copy=retain_local_copy
            )
            uploaded.append(key)

    await asyncio.gather(*[_upload_one(k, p) for k, p in files])
    return uploaded


async def upload_file_from_bytes(
    key: str,
    content: bytes,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> str:
    """Upload bytes directly to *key* in the store.

    Writes content to a temporary file, uploads it, then cleans up.

    Args:
        key: Destination object key.
        content: Bytes to upload.
        store: Target store, or ``None`` to use the infrastructure store.
        normalize: When ``True`` (default), normalise *key* before use.

    Returns:
        Hex-encoded SHA-256 digest of the uploaded content.
    """
    import tempfile  # noqa: PLC0415 — stdlib tempfile; lazy use only

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        return await upload_file(key, tmp_path, store, normalize=normalize)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
