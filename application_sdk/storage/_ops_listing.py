"""Object listing for :mod:`application_sdk.storage.ops`, directory markers filtered.

:func:`_list_items` is the one listing primitive the storage package uses; the
rest of this module is how it recognises a directory marker (a GCS console
"folder" or an S3 ``folder/`` key) so callers never download or count one,
and :func:`_confirm_head_miss`, the listing that tells a missing key from a
gateway's bodiless HEAD 404.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

import obstore

# obstore's typed not-found class; the ``obstore>=0.12`` pin guarantees it.
from obstore.exceptions import NotFoundError as _ObstoreNotFoundError

from application_sdk._runtime.offload import run_in_thread
from application_sdk.storage import _settings
from application_sdk.storage._ops_errors import _storage_error_for
from application_sdk.storage._ops_seam import (
    _is_gateway_no_route,
    _past_gateway_no_route,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore


async def _list_items(
    store: ObjectStore,
    prefix: str | None,
    *,
    include_markers: bool = False,
    suffixes: Iterable[str] = (),
) -> list[tuple[str, int, str | None]]:
    """Collect listing results under *prefix*, optionally filtering GCS directory markers.

    Makes a single listing operation (``obstore.list`` returns a native async
    ``ListStream`` that pages internally — no thread wrapping needed).  When *include_markers* is
    ``False``, two additional in-memory passes are applied: one to build the set of
    ancestor path segments, and one to filter out zero-byte objects whose path is one
    of those ancestors (the structural signature of a GCS-console "folder" marker).

    A zero-byte object is excluded when its path is a strict path-prefix of at least
    one other listed key — i.e. it acts as a parent directory for real files.
    Before that filter, each zero-byte object is probed with a HEAD; one that is
    not found and lists under its own path is an S3 ``folder/`` marker listed
    without its trailing slash and is dropped, so it is neither returned nor
    counted as a parent.

    Args:
        store: An obstore-compatible store instance.
        prefix: Key prefix, or ``None`` to list everything.
        include_markers: When ``True``, skip the directory-marker filter and return
            every object including zero-byte markers.  Use this when the caller must
            operate on *all* objects (e.g. ``delete_prefix``) so that no orphan
            objects are left behind on any store backend.
        suffixes: Case-insensitive key suffixes the caller keeps. When given,
            only zero-byte objects ending in one of them are probed.

    Returns:
        ``(path, size, e_tag)`` tuples in listing order (``e_tag`` may be
        ``None`` on stores that don't provide one).  Directory markers are
        excluded unless *include_markers* is ``True``.
    """

    async def _collect() -> list[tuple[str, int, str | None]]:
        # A retry restarts the listing from the first page, so a partial
        # result never leaks into the next attempt.
        items: list[tuple[str, int, str | None]] = []
        async for batch in obstore.list(
            store, prefix=prefix
        ):  # native async ListStream
            for item in batch:
                items.append((str(item["path"]), int(item["size"]), item.get("e_tag")))
        return items

    all_items = await _past_gateway_no_route("list", prefix or "", _collect)

    if include_markers:
        return all_items
    # Offloaded: two passes over the whole listing with no await in between,
    # which at 100k keys holds the event loop (and the activity heartbeat).
    phantoms = await _childless_marker_paths(store, all_items, suffixes)
    return await run_in_thread(_drop_markers, all_items, phantoms)


def _drop_markers(
    all_items: list[tuple[str, int, str | None]],
    phantoms: set[str],
) -> list[tuple[str, int, str | None]]:
    """Drop *phantoms*, then the directory markers among the remaining items."""
    if phantoms:
        all_items = [item for item in all_items if item[0] not in phantoms]
    return _drop_directory_markers(all_items)


async def _childless_marker_paths(
    store: ObjectStore,
    items: list[tuple[str, int, str | None]],
    suffixes: Iterable[str],
) -> set[str]:
    """Return zero-byte listed paths that are S3 ``folder/`` markers.

    An S3 ``folder/`` marker lists without its trailing slash, so a HEAD of its
    listed path finds nothing. A real zero-byte key deleted after the listing
    finds nothing too, so a not-found HEAD is confirmed by listing under the
    path: the marker lists there as itself, the vanished key lists nothing and
    is kept, so its download reports it. Only the error class decides
    not-found: obstore error text carries the key, so a substring match would
    misread a key like ``dt=20240404/_SUCCESS`` on a 403 or 503.

    A fixed pool of workers drains the candidates, so a listing with 100k
    zero-byte keys holds only that many tasks, not one per key.
    """
    candidates = await run_in_thread(
        _zero_byte_paths, items, tuple(suffix.lower() for suffix in suffixes)
    )
    if not candidates:
        return set()
    from application_sdk.storage._concurrency import _run_bounded  # noqa: PLC0415

    max_concurrent_transfers = _settings.max_concurrent_storage_transfers()

    markers: set[str] = set()
    pending = iter(candidates)

    async def _worker() -> None:
        # One shared iterator: each worker takes the next path when it is free.
        for path in pending:
            if await _head_not_found(store, path) and await _lists_under_itself(
                store, path
            ):
                markers.add(path)

    workers = min(max(max_concurrent_transfers, 1), len(candidates))
    await _run_bounded([_worker() for _ in range(workers)], None)
    return markers


async def _head_not_found(store: ObjectStore, path: str) -> bool:
    """True only when a HEAD of *path* fails with a not-found error class."""
    try:
        # Through the seam: obstore raises its not-found class for the
        # gateway's no-route 404 too, which would read a real key as absent.
        await _past_gateway_no_route(
            "head", path, lambda: obstore.head_async(store, path)
        )
    except FileNotFoundError as exc:
        # An exhausted gateway retry re-raises the no-route 404, which obstore
        # also types as not-found: that is the gateway, not the key.
        return not _is_gateway_no_route(exc)
    except Exception as exc:  # noqa: BLE001 — probe only; any other error keeps the key and the download reports it
        if _is_gateway_no_route(exc):
            return False
        return isinstance(exc, _ObstoreNotFoundError)
    return False


async def _lists_under_itself(store: ObjectStore, path: str) -> bool:
    """True when listing under *path* returns anything.

    obstore lists *path* as the prefix ``path/``, which holds an S3
    ``path/`` marker (listed as *path*) but never a plain key named *path*.
    """

    async def _probe() -> bool:
        async for batch in obstore.list(store, prefix=path, chunk_size=1):
            if batch:
                return True
        return False

    try:
        # Through the seam, like the HEAD probe: a stale gateway instance here
        # would otherwise leave an S3 ``path/`` marker in the listing.
        return await _past_gateway_no_route("list", path, _probe)
    except Exception:  # noqa: BLE001 — probe only; any error keeps the key and the download reports it
        return False


def _zero_byte_paths(
    items: list[tuple[str, int, str | None]],
    lsuffixes: tuple[str, ...],
) -> list[str]:
    """Zero-byte listed paths, limited to *lsuffixes* (lowercase) when given."""
    return [
        path
        for path, size, _ in items
        if size == 0 and (not lsuffixes or path.lower().endswith(lsuffixes))
    ]


def _drop_directory_markers(
    all_items: list[tuple[str, int, str | None]],
) -> list[tuple[str, int, str | None]]:
    """Drop zero-byte objects whose path is an ancestor of another listed key."""
    parent_dirs: set[str] = set()
    for path, _, _ in all_items:
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent_dirs.add("/".join(parts[:i]))

    return [
        (path, size, etag)
        for path, size, etag in all_items
        if not (size == 0 and path in parent_dirs)
    ]


async def _confirm_head_miss(
    resolved: ObjectStore,
    key: str,
    listing_cache: dict[str, dict[str, tuple[int, str | None]]] | None,
) -> tuple[int, str | None] | None:
    """Look *key* up in a listing of its parent, after a HEAD said it is absent.

    It lists the parent folder because a listing prefix matches whole path
    segments: listing the key itself never returns the key. A root-level key
    has no parent segment, so it lists the bucket root (no prefix) rather than
    ``"/"``, which only means the root because obstore's path parsing happens
    to strip it.

    The listing is raw (``include_markers=True``). The question is whether an
    object sits at exactly *key*, and the directory-marker filter answers a
    different one: it drops a zero-byte key that has children, and decides
    which zero-byte keys are phantom markers by HEADing them — the very HEAD a
    gateway without routes answers with a bodiless 404. Filtered, a real
    zero-byte ``dir`` beside ``dir/child`` would still read as absent.
    """
    parent = key.rpartition("/")[0]
    list_prefix = parent + "/" if parent else ""
    found = listing_cache.get(list_prefix) if listing_cache is not None else None
    if found is None:
        try:
            items = await _list_items(
                resolved, list_prefix or None, include_markers=True
            )
        # conformance: ignore[E004] always re-raises as StorageError; a listing that fails is not evidence the key is absent
        except Exception as exc:
            raise _storage_error_for(
                exc,
                list_prefix,
                f"Failed to list prefix '{list_prefix}' to confirm a HEAD miss "
                f"on key '{key}'",
                resolved,
            ) from exc
        found = {path: (size, etag) for path, size, etag in items}
        if listing_cache is not None:
            listing_cache[list_prefix] = found
    return found.get(key)
