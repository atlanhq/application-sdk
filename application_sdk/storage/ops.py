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

Migration from v2
-----------------
* ``objectstore.get_content(key)``  →  ``get_bytes(key)``  (or ``get_content(key)``)
* ``objectstore.upload_bytes(key, data)``  →  ``put(key, data)``
* ``objectstore.delete(key)``  →  ``delete(key)``
* ``objectstore.exists(key)``  →  ``exists(key)``
* ``objectstore.list_keys(prefix)``  →  ``list_keys(prefix)``
* ``objectstore.delete_prefix(prefix)``  →  ``delete_prefix(prefix)``

All calls that previously went through the implicit singleton store now
resolve from the infrastructure context automatically.  Pass ``store=my_store``
to target a specific store instead.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import obstore

if TYPE_CHECKING:
    from obstore.store import ObjectStore


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


async def get_bytes(
    key: str,
    store: "ObjectStore | None" = None,
    *,
    normalize: bool = True,
) -> bytes | None:
    """Fetch the bytes stored at *key*, or ``None`` if the key does not exist.

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


#: v2-compatible alias for :func:`get_bytes`.
get_content = get_bytes


async def put(
    key: str,
    data: bytes,
    store: "ObjectStore | None" = None,
    *,
    normalize: bool = True,
) -> None:
    """Write *data* to *key* in the store (creates or overwrites).

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


async def delete_prefix(
    prefix: str,
    store: "ObjectStore | None" = None,
    *,
    normalize: bool = True,
) -> int:
    """Delete all objects whose key starts with *prefix*.

    Mirrors ``ObjectStore.delete_prefix()`` from v2 for migration parity.
    Lists all matching keys then deletes each one individually.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

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


async def list_keys(
    prefix: str = "",
    store: "ObjectStore | None" = None,
    *,
    normalize: bool = True,
) -> list[str]:
    """List all object keys under *prefix*.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        prefix: Key prefix to filter by.  Empty string lists all keys.
            Normalised by default (see :func:`normalize_key`).  A trailing
            ``/`` is preserved (or added) after normalisation so that prefix
            matching never bleeds into sibling directories
            (e.g. ``"artifacts"`` won't match ``"artifacts_backup/"``).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *prefix* before use.
            Pass ``False`` to use *prefix* exactly as supplied.

    Returns:
        Sorted list of matching object keys.

    Raises:
        StorageError: If the listing fails.
        RuntimeError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize and prefix:
        prefix = normalize_key(prefix)
        # Ensure trailing slash so the prefix matches only its own subtree.
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"
    try:
        keys: list[str] = []
        for batch in obstore.list(resolved, prefix=prefix or None):
            for item in batch:
                keys.append(str(item["path"]))
        return sorted(keys)
    except Exception as exc:
        from application_sdk.storage.errors import StorageError

        raise StorageError(
            f"Failed to list keys with prefix '{prefix}'", cause=exc
        ) from exc


#: v2-compatible alias for :func:`delete`.
delete_file = delete

#: v2-compatible alias for :func:`list_keys`.
list_files = list_keys
