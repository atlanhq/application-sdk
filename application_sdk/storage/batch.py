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
from pathlib import Path
from typing import TYPE_CHECKING

import obstore

from application_sdk.storage.ops import (
    _resolve_store,
    delete,
    download_file,
    normalize_key,
    upload_file,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore


async def list_keys(
    prefix: str = "",
    store: "ObjectStore | None" = None,
    *,
    suffix: str = "",
    normalize: bool = True,
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
            keys ending with this string are returned (e.g. ``".parquet"``).
        normalize: When ``True`` (default), normalise *prefix* before use.
            Pass ``False`` to use *prefix* exactly as supplied.

    Returns:
        Sorted list of matching object keys.

    Raises:
        StorageError: If the listing fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    import asyncio

    resolved = _resolve_store(store)
    if normalize and prefix:
        prefix = normalize_key(prefix)
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"

    def _collect() -> list[str]:
        keys: list[str] = []
        for batch in obstore.list(resolved, prefix=prefix or None):
            for item in batch:
                key = str(item["path"])
                if not suffix or key.endswith(suffix):
                    keys.append(key)
        return sorted(keys)

    try:
        return await asyncio.to_thread(_collect)
    except Exception as exc:
        from application_sdk.storage.errors import StorageError

        raise StorageError(
            f"Failed to list keys with prefix '{prefix}'", cause=exc
        ) from exc


async def delete_prefix(
    prefix: str,
    store: "ObjectStore | None" = None,
    *,
    normalize: bool = True,
) -> int:
    """Delete all objects whose key starts with *prefix*.

    Lists all matching keys then deletes each one individually.

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
    keys = await list_keys(prefix, resolved, normalize=normalize)
    count = 0
    for key in keys:
        if await delete(key, resolved, normalize=False):
            count += 1
    return count


async def download_prefix(
    prefix: str,
    local_dir: "str | Path",
    store: "ObjectStore | None" = None,
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
    import asyncio

    keys = await list_keys(prefix, store, suffix=suffix, normalize=normalize)
    local = Path(local_dir)
    destinations = [str(local / key) for key in keys]

    sem = asyncio.Semaphore(max_concurrency)

    async def _download_one(key: str, dest: str) -> None:
        async with sem:
            await download_file(key, dest, store, normalize=False)

    await asyncio.gather(*[_download_one(k, d) for k, d in zip(keys, destinations)])
    return destinations


async def upload_prefix(
    local_dir: "str | Path",
    prefix: str,
    store: "ObjectStore | None" = None,
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
    import asyncio

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
            key = f"{prefix}/{rel}" if prefix else str(rel)
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
    store: "ObjectStore | None" = None,
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
    import tempfile

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
