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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import obstore

from application_sdk._runtime.offload import run_in_thread
from application_sdk.common._listing import prune_internal_dirs
from application_sdk.observability.logger_adaptor import get_logger

# Sidecar naming is owned by ``storage.integrity`` — the module that also reads
# and writes them. Re-exported here (``batch.SIDECAR_SUFFIX`` /
# ``batch.is_sidecar_key``) because the listing filters are the historical
# import site and ``application_sdk.storage`` re-exports both from batch.
from application_sdk.storage.integrity import (
    SIDECAR_SUFFIX as SIDECAR_SUFFIX,  # re-export
)
from application_sdk.storage.integrity import is_sidecar_key, sidecar_key
from application_sdk.storage.ops import (
    _is_local_dir_collision,
    _is_not_found,
    _list_items,
    _normalize_listing_prefix,
    _resolve_store,
    _safe_join_under,
)
from application_sdk.storage.ops import delete as _delete_object
from application_sdk.storage.ops import (
    download_file_chunked,
    normalize_key,
    upload_file,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.storage.ops import BoundStore

logger = get_logger(__name__)


async def list_keys(
    prefix: str = "",
    store: BoundStore | ObjectStore | None = None,
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
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    prefix = _normalize_listing_prefix(prefix, normalize)

    try:
        items = await _list_items(
            resolved, prefix or None, include_markers=include_markers
        )
        lsuffix = suffix.lower() if suffix else ""
        return sorted(
            path
            for path, _, _ in items
            if not lsuffix or path.lower().endswith(lsuffix)
        )
    # conformance: ignore[E004] always re-raises as StorageError; no logging needed at this layer
    except Exception as exc:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(
            f"Failed to list keys with prefix '{prefix}'", cause=exc
        ) from exc


async def list_keys_with_meta(
    prefix: str = "",
    store: BoundStore | ObjectStore | None = None,
    *,
    suffix: str = "",
    normalize: bool = True,
) -> list[tuple[str, int, str | None]]:
    """Like :func:`list_keys`, but return ``(key, size_bytes, e_tag)`` tuples.

    The listing already carries each object's size and etag, so callers that
    need to decide *per file* whether to chunk a download (large object) or
    stream it (small object) — and to version-pin the chunked range GETs —
    can do so without a follow-up HEAD per key (BLDX-1513 / BLDX-1523).
    ``e_tag`` may be ``None`` on stores that don't provide one.
    Directory-marker filtering, suffix filtering, and sort order match
    :func:`list_keys`.

    Raises:
        StorageError: If the listing fails.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    prefix = _normalize_listing_prefix(prefix, normalize)

    try:
        items = await _list_items(resolved, prefix or None)
        lsuffix = suffix.lower() if suffix else ""
        return sorted(
            (path, size, etag)
            for path, size, etag in items
            if not lsuffix or path.lower().endswith(lsuffix)
        )
    # conformance: ignore[E004] always re-raises as StorageError; no logging needed at this layer
    except Exception as exc:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(
            f"Failed to list keys with prefix '{prefix}'", cause=exc
        ) from exc


@dataclass(frozen=True, slots=True)
class DataObject:
    """One data object from a listing, with everything a transfer needs.

    ``has_sidecar`` is free here and expensive later: the listing has already
    enumerated every key under the prefix, including the ``{key}.sha256``
    sidecars, so pairing them up costs one in-memory pass. A download that does
    not carry the answer forward has to probe for the sidecar per file instead
    — one extra round trip each, on a path that runs thousands of times.
    """

    key: str
    size: int
    etag: str | None
    has_sidecar: bool


async def list_data_objects(
    prefix: str = "",
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> list[DataObject]:
    """List data objects under *prefix*, pairing each with its sidecar flag.

    The single listing pass every sidecar-aware helper is built on: it applies
    the ``is_sidecar_key`` exclusion once and records, per surviving data
    object, whether its integrity sidecar was in the same listing.

    Returns:
        Sorted list of :class:`DataObject` for data objects only.
    """
    items = await list_keys_with_meta(prefix, store, normalize=normalize)
    all_keys = {k for k, _, _ in items}
    return [
        DataObject(
            key=key,
            size=size,
            etag=etag,
            has_sidecar=sidecar_key(key) in all_keys,
        )
        for key, size, etag in items
        if not is_sidecar_key(key)
    ]


async def list_data_keys(
    prefix: str = "",
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> list[str]:
    """List data object keys under *prefix*, excluding SHA-256 sidecars.

    Thin wrapper over :func:`list_keys` that drops ``{key}.sha256`` sidecar
    entries, so callers enumerating a directory's *content* (upload reconcile,
    deployment-store fallback, …) share one definition of "data key". See
    :func:`list_data_objects` when per-object size / etag / sidecar presence is
    also needed.

    Unlike :func:`list_keys`, this does not expose a ``suffix`` filter: the sole
    narrowing here is the sidecar exclusion. That omission is intentional — no
    call site needs an additional suffix filter, so callers hand-filter the
    returned list on the rare occasion they want one.

    Args:
        prefix: Key prefix to list under (see :func:`list_keys`).
        store: Object store, or ``None`` to resolve from infrastructure context.
        normalize: When ``True`` (default), normalise *prefix* before use.

    Returns:
        Sorted list of data object keys with sidecars removed.
    """
    return [
        k
        for k in await list_keys(prefix, store, normalize=normalize)
        if not is_sidecar_key(k)
    ]


async def list_data_keys_with_meta(
    prefix: str = "",
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> list[tuple[str, int, str | None]]:
    """Like :func:`list_data_keys`, but return ``(key, size_bytes, e_tag)`` tuples.

    Drops SHA-256 sidecars from :func:`list_keys_with_meta` so download /
    materialize paths that need per-object size + etag (to chunk large objects
    without a per-file HEAD) share the same sidecar-exclusion rule.

    Like :func:`list_data_keys`, this intentionally does not expose a ``suffix``
    filter; sidecar exclusion is the only narrowing.

    Returns:
        Sorted list of ``(key, size_bytes, e_tag)`` for data objects only.
    """
    return [
        (obj.key, obj.size, obj.etag)
        for obj in await list_data_objects(prefix, store, normalize=normalize)
    ]


async def delete_prefix(
    prefix: str,
    store: ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> int:
    """Delete all objects whose key starts with *prefix*.

    Uses the store's native bulk-delete API where available (S3 batches up to
    1 000 keys per request; Azure up to 256; GCS issues 10 parallel individual
    DELETE requests).  A not-found error after the fresh listing means a key
    vanished between the two calls — the desired end state for that key is
    reached either way, so it is benign: it is logged as a warning (it can
    indicate two apps sharing the prefix) and the delete falls back to an
    idempotent per-key pass (FND-341).  Every other error is fatal.

    Args:
        prefix: Key prefix — all objects under this prefix are deleted.
            Normalised by default (see :func:`normalize_key`).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *prefix* before use.

    Returns:
        Number of objects deleted.  Keys that vanished concurrently are not
        counted — only objects this call actually removed.

    Raises:
        StorageError: If the listing fails, or if a deletion fails for any
            reason other than the key already being gone.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
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
    # conformance: ignore[E004] always re-raises as StorageError; no logging needed at this layer
    except Exception as exc:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(
            f"Failed to list keys with prefix '{prefix}'", cause=exc
        ) from exc

    paths = [path for path, _, _ in items]

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
        # conformance: ignore[E004] probe for directory marker existence; not-found is expected and swallowed intentionally
        except Exception as exc:
            # A local store maps the bare marker key onto the directory the
            # prefix holds, and its stat does not surface as "not found" (on
            # Windows it is a GenericError "Access is denied") — that is a
            # directory collision (no marker object), not a permission error.
            # The store and key are threaded in so the relaxation is decided by
            # what the key actually resolves to, not by the message wording.
            if not (
                _is_not_found(exc)
                or _is_local_dir_collision(exc, resolved, root_marker)
            ):
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
    # conformance: ignore[E004] not-found is the benign list/delete race handled below (logged + retried per key); every other error re-raises as StorageError
    except Exception as exc:
        if not _is_not_found(exc):
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                StorageError,
            )

            raise StorageError(
                f"Failed to delete {len(paths)} objects with prefix '{prefix}'",
                cause=exc,
            ) from exc

        # A key vanished between the listing and the bulk delete (GCS and Azure
        # report a per-key not-found; S3 and MemoryStore treat it as success).
        # The end state this call wants — the object gone — is already true for
        # that key, so failing the caller over it turns a benign race into a
        # workflow failure (FND-341).  It *can* mean two apps are writing the
        # same prefix, which is worth seeing, so say so at WARNING instead.
        logger.warning(
            "Object under prefix '%s' vanished between listing and bulk delete; "
            "retrying %d key(s) individually. This is benign on its own, but can "
            "indicate two apps writing the same prefix. Store error: %s",
            prefix,
            len(paths),
            exc,
            exc_info=True,
        )
        # obstore's bulk delete is all-or-nothing to the caller: it names at most
        # one key and stops at the first per-key failure, so neither which keys
        # are gone nor which are still there is knowable from here.  Re-run the
        # deletes one key at a time — ``ops.delete`` is idempotent (``False`` on
        # not-found) and still fatal on real errors — so the prefix ends up empty
        # and the count reflects only what this call removed.
        return await _delete_paths_individually(resolved, paths)

    return len(paths)


async def _delete_paths_individually(
    store: ObjectStore,
    paths: list[str],
    *,
    max_concurrency: int = 10,
) -> int:
    """Delete *paths* one key at a time, skipping keys that are already gone.

    Fallback path for the benign list/delete race in :func:`delete_prefix`; the
    default concurrency matches obstore's own fan-out for stores without a
    native bulk delete.

    Returns:
        Number of keys that existed and were deleted.

    Raises:
        StorageError: If a delete fails for any reason other than not-found.
            The first failure also cancels the remaining per-key deletes and
            *waits* for that cancellation to finish before propagating, so no
            sibling delete outlives the raised error.
    """
    import asyncio  # noqa: PLC0415 — stdlib asyncio; lazy use only

    sem = asyncio.Semaphore(max_concurrency)

    async def _delete_one(path: str) -> bool:
        async with sem:
            return await _delete_object(path, store, normalize=False)

    # A TaskGroup (vs gather) gives structured cancel-and-await semantics: on
    # the first StorageError the group cancels the remaining per-key tasks and
    # does not return until they have actually finished unwinding, so no delete
    # is left in flight to complete after the caller has seen the failure.  The
    # group wraps the failure in an ExceptionGroup; unwrap the StorageError so
    # the caller sees the original contract (a bare StorageError), not a group.
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageError,
    )

    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(_delete_one(p)) for p in paths]
    except BaseExceptionGroup as group:
        # Unwrap only a group that is *entirely* StorageError. Picking the first
        # StorageError out of a mixed group would demote every other leaf to
        # ``__cause__`` — reachable in a traceback, invisible to an
        # ``except`` clause. ops.delete wraps every per-key failure, so the
        # homogeneous case is the only one that happens in practice; a foreign
        # leaf means something unforeseen and is better surfaced as the group.
        if all(isinstance(leaf, StorageError) for leaf in group.exceptions):
            raise group.exceptions[0] from group
        raise
    return sum(t.result() for t in tasks)


def _local_relative_key(key: str, strip: str) -> str:
    """Return the path *key* should occupy under a download destination.

    With *strip* empty the key is used whole (full-store-path layout). Otherwise
    *strip* — the normalised listing prefix, without its trailing ``/`` — is
    removed, so the tree *under* the prefix is what lands locally.

    The strip is boundary-aware: only a key that *is* the prefix or sits under
    it (``<strip>/...``) is stripped. A bare ``startswith`` match would also
    mis-strip a sibling key sharing a string prefix (``a/b2/x`` under strip
    ``a/b`` → ``2/x``) — reachable when the caller passes ``normalize=False``
    with a slash-less prefix, where the listing itself is not boundary-safe.
    """
    if not strip or not (key == strip or key.startswith(strip + "/")):
        return key
    relative = key[len(strip) :].lstrip("/")
    # A key that *is* the prefix (only reachable with normalize=False and a
    # slash-less prefix) leaves nothing to join, which would target the
    # destination directory itself — keep its basename so a file is written.
    return relative or PurePosixPath(key).name


async def download_prefix(
    prefix: str,
    local_dir: str | Path,
    store: ObjectStore | None = None,
    *,
    suffix: str = "",
    normalize: bool = True,
    strip_prefix: bool = False,
    max_concurrency: int = 4,
) -> list[str]:
    """Download all objects under *prefix* to a local directory.

    *strip_prefix* selects between the two layouts:

    * ``False`` (default) — each key's full store path is preserved under
      *local_dir* (key ``artifacts/run/file.json`` →
      ``local_dir/artifacts/run/file.json``).
    * ``True`` — *prefix* is stripped, so the tree beneath it is reproduced
      directly in *local_dir* (key ``artifacts/run/file.json`` under prefix
      ``artifacts/run`` → ``local_dir/file.json``).

    Pass ``strip_prefix=True`` whenever *local_dir* already names the same
    directory as *prefix* — otherwise the prefix appears twice in the result
    (``<out>/transformed/artifacts/.../transformed/table/``) and any reader that
    looks for a fixed subpath such as ``<out>/transformed/table`` finds nothing
    (FND-340). It is the exact inverse of
    :func:`upload_prefix` ``(local_dir, prefix)``, which writes each file's path
    *relative to* ``local_dir`` under ``prefix``.

    Downloads run concurrently (up to *max_concurrency* at a time).

    ``{key}.sha256`` sidecars are not mirrored to disk: they are SDK
    bookkeeping, and a caller that hands the downloaded directory to a reader
    which globs it (a RocksDB / DuckDB state directory, a parquet dataset) must
    not find extra files in it. They are instead consumed here — each object is
    verified against its sidecar as it lands (FND-306).

    Args:
        prefix: Object key prefix to download.
        local_dir: Local directory to write files into.
        store: Source store, or ``None`` to use the infrastructure store.
        suffix: Optional extension filter (e.g. ``".parquet"``).
        normalize: When ``True`` (default), normalise *prefix* before use.
        strip_prefix: When ``True``, drop *prefix* from each key so only the
            tree below it is written under *local_dir*.  Defaults to ``False``
            (full store path preserved).
        max_concurrency: Maximum parallel downloads (default 4).

    Returns:
        List of local file paths that were downloaded.

    Raises:
        StorageError: If listing or downloading fails.
        StorageIntegrityError: If an object does not match its sidecar digest.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    import asyncio  # noqa: PLC0415 — stdlib asyncio; lazy use only

    objects = await list_data_objects(prefix, store, normalize=normalize)
    if suffix:
        lsuffix = suffix.lower()
        objects = [o for o in objects if o.key.lower().endswith(lsuffix)]
    local = Path(local_dir)
    # Strip against the *normalised* listing prefix rather than the caller's raw
    # argument: normalisation is what the listing matched on, so a v2-style
    # "./local/tmp/artifacts/..." prefix strips exactly like its "artifacts/..."
    # key form.
    strip = (
        _normalize_listing_prefix(prefix, normalize).rstrip("/") if strip_prefix else ""
    )
    # Reject keys whose resolved path escapes local_dir (e.g. via ".." segments).
    destinations = [
        str(_safe_join_under(local, _local_relative_key(obj.key, strip)))
        for obj in objects
    ]

    sem = asyncio.Semaphore(max_concurrency)

    async def _download_one(obj: DataObject, dest: str) -> None:
        async with sem:
            # Pass the listing's size + etag so a large object is fetched via
            # bounded parallel range GETs (each with its own timeout / retry
            # budget, version-pinned via If-Match) while small objects still
            # stream in a single GET — and no per-file HEAD is issued, since
            # the metadata is already known. (BLDX-1513 / BLDX-1523)
            # has_sidecar comes from the same listing, so verification costs at
            # most the one GET that actually reads the digest.
            await download_file_chunked(
                obj.key,
                dest,
                store,
                normalize=False,
                file_size=obj.size,
                etag=obj.etag,
                sidecar_present=obj.has_sidecar,
            )

    await asyncio.gather(
        *[_download_one(obj, d) for obj, d in zip(objects, destinations)]
    )
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
    Symlinks are skipped to prevent path-traversal, and SDK working
    directories (:data:`~application_sdk.common._listing.INTERNAL_DIRNAMES`) are
    not descended into — an artifact still being staged by an atomic write must
    not be uploaded as though it were finished (FND-318).

    Note:
        Param order is ``(local_dir, prefix)`` — source first, destination second.
        This is the inverse of :func:`download_prefix` ``(prefix, local_dir)`` which
        also follows source-first convention. The *layout* round-trips only with
        ``download_prefix(..., strip_prefix=True)``; the default download keeps
        each key's full store path, which nests the prefix under *local_dir*.

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

    def _collect_files() -> list[tuple[str, Path]]:
        collected: list[tuple[str, Path]] = []
        for root, dirs, filenames in os.walk(local, followlinks=False):
            prune_internal_dirs(dirs)
            for fname in filenames:
                file_path = Path(root) / fname
                if file_path.is_symlink():
                    continue
                rel = file_path.relative_to(local)
                # Use PurePosixPath to ensure forward slashes in S3 keys (Windows uses backslash)
                rel_posix = PurePosixPath(*rel.parts)
                key = f"{prefix}/{rel_posix}" if prefix else str(rel_posix)
                collected.append((key, file_path))
        return collected

    # Offloaded: the walk is one stat per entry over a whole run's output
    # directory, which on a large extraction is thousands of syscalls with no
    # await between them. Inline it holds the event loop — and the enclosing
    # activity's auto-heartbeat — for the entire traversal (ADR-0010).
    files: list[tuple[str, Path]] = await run_in_thread(_collect_files)

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
        sha256 = await upload_file(key, tmp_path, store, normalize=normalize)
        # compute_hash defaults to True, so upload_file always returns the digest here.
        assert sha256 is not None
        return sha256
    finally:
        try:
            os.unlink(tmp_path)
        # conformance: ignore[E002] best-effort temp-file cleanup; not fatal
        except OSError:
            pass
