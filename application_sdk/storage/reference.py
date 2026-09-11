"""FileReference persist / materialize operations.

A ``FileReference`` can be in one of two states:

* **Ephemeral** (``is_durable=False``): the data lives only on the local
  filesystem (``local_path`` is set).  This is safe to pass within a single
  activity, but cannot survive a Temporal payload round-trip because the
  remote worker won't have the file.

* **Durable** (``is_durable=True``): the data has been uploaded to the
  object store (``storage_path`` is set).  The reference can be serialised
  into a Temporal payload and materialised on any worker.

``persist_file_reference`` transitions ephemeral → durable.
``materialize_file_reference`` transitions durable → local (downloads the
file to a temp path when ``local_path`` is absent or cannot be verified).

SHA-256 sidecars
----------------
Both functions maintain a ``{path}.sha256`` sidecar alongside every file:

* **persist**: computes sha256 of the local file via streaming upload,
  writes ``{storage_path}.sha256`` to the store, and writes
  ``{local_path}.sha256`` locally.
* **materialize**: before downloading a single file, checks whether the
  local file (if present) already matches the stored sidecar.  If so,
  writes the local sidecar and returns without re-downloading.  Otherwise
  downloads the file via streaming, verifies integrity against the stored
  sidecar (if available), then writes the local sidecar.

For directory references, each file within the prefix is checked individually
against its local sidecar before downloading.  Files whose hash matches are
skipped; only changed or absent files are re-downloaded.

The conservative default is: **no stored sidecar → re-download**.  Once a
sidecar exists, subsequent calls on the same worker skip the download
entirely.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from application_sdk._runtime.offload import run_in_thread
from application_sdk.common._listing import safe_list_directory
from application_sdk.common.atomic import atomic_write
from application_sdk.contracts.types import FileReference
from application_sdk.storage._locks import PathLockRegistry

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.storage.batch import DataObject

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage import integrity

logger = get_logger(__name__)

# Files at or above this size emit transfer events at INFO; smaller files use DEBUG.
_INFO_LOG_THRESHOLD = 10 * 1024 * 1024  # 10 MiB


def _make_storage_path(ref: FileReference, *, output_path: str | None = None) -> str:
    """Generate a unique storage path for a single-file FileReference.

    Delegates to :meth:`StorageTier._make_file_ref_path`.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
        APPLICATION_NAME,
    )

    suffix = Path(ref.local_path).suffix if ref.local_path else ""
    return ref.tier._make_file_ref_path(
        suffix=suffix,
        run_prefix=output_path or "",
        app_name=APPLICATION_NAME,
    )


def _make_storage_prefix(ref: FileReference, *, output_path: str | None = None) -> str:
    """Generate a unique storage prefix for a directory FileReference.

    Delegates to :meth:`StorageTier._make_file_ref_prefix`.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
        APPLICATION_NAME,
    )

    return ref.tier._make_file_ref_prefix(
        run_prefix=output_path or "",
        app_name=APPLICATION_NAME,
    )


#: Per-destination mutex for the whole materialise-and-verify step.
#:
#: ``local_path`` is a deterministic function of (run, stage, entity), so
#: concurrent activities of one run share the destination by construction
#: (CONNECT-1126). Serialising on it is the dedupe mechanism, not the safety
#: mechanism — the transfer layer's atomic publish is what keeps concurrent
#: readers safe; the lock makes the second caller wait, re-check the
#: now-complete file against its sidecar, and skip the duplicate download.
#: A separate registry from the transfer layer's, so holding this guard and
#: calling into ``download_file_chunked`` never self-deadlocks.
_MATERIALIZE_LOCKS = PathLockRegistry("file_ref.materialize.lock_wait")

_materialize_lock = _MATERIALIZE_LOCKS.lock
_materialize_guard = _MATERIALIZE_LOCKS.guard


def _write_local_sidecar(local_path: str, sha256: str) -> None:
    """Write a local ``.sha256`` sidecar next to *local_path*.

    Atomic even though the write is best-effort, and *because* it is: a
    truncated digest is not a missing sidecar, it is a wrong one. A later
    ``materialize`` would compare a good local file against a partial digest,
    conclude the file is stale, and re-download it every time — a silent,
    permanent tax rather than the visible failure a missing sidecar produces
    (FND-318).
    """
    try:
        with atomic_write(
            local_path + ".sha256", operation="local sidecar write"
        ) as sidecar:
            sidecar.write(sha256.encode())
    except Exception:
        logger.warning(
            "Sidecar write failed (best-effort, continuing without)", exc_info=True
        )


async def persist_file_reference(
    store: ObjectStore,
    ref: FileReference,
    *,
    key: str | None = None,
    output_path: str | None = None,
) -> FileReference:
    """Upload the local file or directory referenced by *ref* to *store*.

    For single files, performs a streaming upload and writes a
    ``{storage_path}.sha256`` sidecar to the store and a
    ``{local_path}.sha256`` sidecar locally so that subsequent
    ``materialize_file_reference`` calls can verify integrity without
    re-downloading.

    For directories, walks the directory tree, uploads each file under a
    generated prefix, and writes per-file sidecars.

    Args:
        store: Destination obstore store.
        ref: An ephemeral ``FileReference`` with ``local_path`` set.
        key: Explicit object-store key (single files only). Precedence:
            ``key`` → ``ref.storage_path`` → UUID-keyed
            ``file_refs/<uuid>.json`` fallback. Pass ``None`` (default)
            to let ``ref.storage_path`` or the fallback decide.
        output_path: Run-scoped base prefix (e.g.
            ``artifacts/apps/{app}/workflows/{wf_id}/{run_id}``).  Required
            when ``ref.tier`` is ``StorageTier.RETAINED``; ignored otherwise.

    Returns:
        A new durable ``FileReference`` (``is_durable=True``) pointing to
        the same data in the store.

    Raises:
        StorageError: If ``ref.local_path`` is ``None`` or the upload fails.
        ValueError: If ``ref.tier`` is ``RETAINED`` and *output_path* is not
            provided.
    """
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageError,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        upload_file,
    )

    if ref.is_durable:
        return ref  # already persisted — nothing to do

    if ref.local_path is None:
        raise StorageError(
            "Cannot persist FileReference: local_path is None",
            key=key,
        )

    local = Path(ref.local_path)

    # Structured kwargs in the logger calls below are intentional: every key used
    # (storage_path, local_path, file_count, file_size_bytes, bytes_uploaded,
    # bytes_transferred_before_failure, sha256, tier, error_type, duration_ms) is
    # in _KNOWN_EXTRA_KEYS (logger_adaptor.py "FileReference transfers", lines 106-126).
    # _build_extra_dict promotes them to top-level OTLP attributes — indexed columns in
    # Grafana+ClickHouse. Do not rewrite to %-style; that would lose the promotion.
    if local.is_dir():
        # ── Directory upload ───────────────────────────────────────────────
        prefix = _make_storage_prefix(ref, output_path=output_path)
        # run_in_thread keeps the blocking fsync + scandir off the event loop,
        # using the dedicated pool rather than asyncio's default executor.
        files = await run_in_thread(safe_list_directory, local)
        _t0 = time.monotonic()
        # conformance: ignore[L018] keys are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
        logger.info(
            "file_ref.persist.start",
            local_path=ref.local_path,
            storage_path=prefix,
            file_count=len(files),
            tier=str(ref.tier),
        )

        async def _upload_one(file_path: Path) -> None:
            relative = str(file_path.relative_to(local)).replace(os.sep, "/")
            file_key = f"{prefix}{relative}"
            # upload_file validates the write and writes the store-side
            # ``{key}.sha256`` sidecar itself (FND-306) — one implementation of
            # the sidecar protocol, shared by every upload path.
            await upload_file(file_key, file_path, store, normalize=False)

        try:
            from application_sdk.constants import (  # noqa: PLC0415
                MAX_CONCURRENT_STORAGE_TRANSFERS,
            )
            from application_sdk.storage._concurrency import (  # noqa: PLC0415
                _gather_with_semaphore,
            )

            sem = asyncio.Semaphore(MAX_CONCURRENT_STORAGE_TRANSFERS)
            await _gather_with_semaphore([_upload_one(fp) for fp in files], sem)
        except Exception as exc:
            # conformance: ignore[L018,L009] structured failure event; keys promoted to indexed OTLP attributes via _KNOWN_EXTRA_KEYS; distinct transfer-boundary telemetry not re-emitted by caller
            logger.error(
                "file_ref.persist.failed",
                storage_path=prefix,
                local_path=ref.local_path,
                error_type=type(exc).__name__,
                exc_info=True,
            )
            raise

        # conformance: ignore[L018] keys are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
        logger.info(
            "file_ref.persist.complete",
            storage_path=prefix,
            file_count=len(files),
            duration_ms=int((time.monotonic() - _t0) * 1000),
            tier=str(ref.tier),
        )
        return FileReference(
            local_path=ref.local_path,
            is_durable=True,
            storage_path=prefix,
            file_count=len(files),
            tier=ref.tier,
        )

    else:
        # ── Single file upload ─────────────────────────────────────────────
        # Precedence for the storage key:
        #   1. explicit ``key`` arg (caller-supplied at the persist site)
        #   2. ``ref.storage_path`` set by the activity that produced the
        #      ref — this is how SqlApp pins canonical keys like
        #      ``<run_prefix>/transformed/<entity>/entities.json`` so the
        #      downstream publish step can find the file by entity-type
        #      lookup instead of having to discover UUIDs under
        #      ``file_refs/``. Without this honour-path the activity
        #      interceptor would always auto-generate
        #      ``file_refs/<uuid>`` keys and assets would silently fall
        #      out of the publish set when ``upload_to_atlan``'s
        #      directory walk runs on a different pod than the
        #      transform that produced the file.
        #   3. ``_make_storage_path`` fallback (UUID-based, for refs that
        #      don't have a meaningful entity-type key).
        storage_path = (
            key or ref.storage_path or _make_storage_path(ref, output_path=output_path)
        )
        _file_size = local.stat().st_size
        _log = logger.info if _file_size >= _INFO_LOG_THRESHOLD else logger.debug
        _t0 = time.monotonic()
        _log(
            "file_ref.persist.start",
            local_path=ref.local_path,
            storage_path=storage_path,
            file_size_bytes=_file_size,
            tier=str(ref.tier),
        )

        try:
            # upload_file validates the write and writes the store-side
            # ``{key}.sha256`` sidecar itself (FND-306); only the *local*
            # sidecar — a materialize-time cache key, not part of the store
            # protocol — is this module's business.
            sha256 = await upload_file(storage_path, local, store, normalize=False)
            # compute_hash defaults to True, so the digest is always returned here.
            assert sha256 is not None
            _write_local_sidecar(ref.local_path, sha256)
        except Exception as exc:
            # conformance: ignore[L018,L009] structured failure event; keys promoted to indexed OTLP attributes via _KNOWN_EXTRA_KEYS; distinct transfer-boundary telemetry not re-emitted by caller
            logger.error(
                "file_ref.persist.failed",
                storage_path=storage_path,
                local_path=ref.local_path,
                error_type=type(exc).__name__,
                bytes_uploaded=0,
                exc_info=True,
            )
            raise

        _log(
            "file_ref.persist.complete",
            storage_path=storage_path,
            bytes_uploaded=_file_size,
            duration_ms=int((time.monotonic() - _t0) * 1000),
            sha256=sha256,
            tier=str(ref.tier),
        )
        return FileReference(
            local_path=ref.local_path,
            is_durable=True,
            storage_path=storage_path,
            tier=ref.tier,
        )


async def materialize_file_reference(
    store: ObjectStore,
    ref: FileReference,
    *,
    local_dir: str | None = None,
) -> FileReference:
    """Download the file or directory referenced by *ref* from *store* locally.

    Lists sub-keys under the path to decide whether *ref* is a directory
    prefix; on an empty listing — which a real single object at the exact key
    also produces — a HEAD on that key decides single file vs empty prefix.

    **Single file**: if ``ref.local_path`` is an existing *file* AND the
    stored sha256 sidecar confirms it is intact, the local sidecar is
    (re-)written and the function returns without downloading.

    **Directory**: fast path is always skipped; all files under the prefix
    are re-listed and downloaded.

    **Empty prefix**: nothing under the prefix, and either no object at the
    exact key or only a 0-byte directory marker. A ref whose ``local_path`` is
    a directory is returned unchanged, naming that (normally empty) directory —
    what an empty hand-off *means* is the consumer's judgement, not the storage
    layer's. With no local directory to hand back, ``StorageNotFoundError`` is
    raised as before.

    Args:
        store: Source obstore store.
        ref: A durable ``FileReference`` with ``storage_path`` set.
        local_dir: Optional directory for temp files/dirs (uses the system
            temp dir if ``None``).

    Returns:
        A ``FileReference`` with ``local_path`` pointing to the verified
        file or directory on the local filesystem.

    Raises:
        StorageNotFoundError: If the key resolves to no objects and *ref* has
            no local directory to hand back.
        StorageError: If the downloaded data does not match the stored sidecar.

    Concurrency: a stable destination — ``ref.local_path``, or the resolved
    directory — is serialised under a per-path lock (CONNECT-1126), so
    concurrent activities sharing one ref wait and reuse the file instead of
    downloading underneath each other. A ``local_path``-less single-file ref
    materialises to a fresh private temp name and needs no lock.
    """
    from application_sdk.storage.batch import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        list_data_objects,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        get_file_meta,
    )

    if not ref.is_durable or ref.storage_path is None:
        return ref  # nothing to materialise

    # Determine single-file vs directory by listing sub-keys under the path.
    # Sizes come back with the listing so the directory branch can chunk large
    # files without a per-file HEAD (BLDX-1513).
    # The listing records which objects carry a ``{key}.sha256`` sidecar, so
    # the per-file integrity check below the transfer call costs no extra
    # existence probe (FND-306).
    data_objects = await list_data_objects(ref.storage_path, store)

    if data_objects:
        if ref.local_path is not None:
            local_directory = ref.local_path
        elif local_dir is not None:
            local_directory = local_dir
        else:
            local_directory = tempfile.mkdtemp()
        Path(local_directory).mkdir(parents=True, exist_ok=True)
        async with _materialize_guard(local_directory):
            return await _materialize_directory(
                store, ref, data_objects, local_directory
            )

    # An empty listing does not mean "nothing is there". ``list_keys`` appends a
    # trailing slash before listing, so a real single object at the exact key
    # ALWAYS lists empty here; and some stores (notably GCS under conditional
    # IAM) return an empty listing when the caller merely lacks list permission.
    # The listing cannot tell either of those apart from a directory prefix
    # holding nothing — only a HEAD on the exact key can, so resolve it here and
    # route on that rather than on a local-filesystem guess. Routing on
    # ``Path(ref.local_path).is_dir()`` alone would answer a question about the
    # store with a fact about the local disk: a real single object whose
    # ``local_path`` happened to be a directory would be handed back
    # undownloaded.
    #
    # The HEAD is not new work on the hot path. The single-file branch has always
    # had to make it — the size picks chunked vs streaming, the etag version-pins
    # the range GETs — so it is made once here and handed down. Only the
    # empty-prefix path, which until now crashed, pays a round trip it did not
    # pay before.
    remote_meta = await get_file_meta(ref.storage_path, store, normalize=False)

    # A HEAD that finds 0 bytes does not prove a single file either. Object
    # stores have no directories, so an empty one is represented by a 0-byte
    # marker object, and this SDK puts the marker for ``D/`` at the bare key
    # ``D`` — see ``delete_prefix``'s ``root_marker`` in batch.py. obstore strips
    # the trailing delimiter when it parses a key, so ``HEAD("D/")`` and
    # ``HEAD("D")`` are the same request and both find that marker, while the
    # listing under ``D/`` stays empty. A real empty object and a directory
    # marker are the same zero bytes at the same key, so no probe can separate
    # them; what separates them is the ref itself, below — only a ref that
    # already names a local directory is handed back.
    #
    # Note the asymmetry, which is the point of HEADing at all: a NON-empty
    # object at the exact key is a single file whatever the local path looks
    # like, and is downloaded. Only the 0-byte answer defers to the ref, and
    # there the alternative is not a better outcome — publishing zero bytes over
    # a directory destination can only fail.
    #
    # Keying on a trailing ``/`` in ``storage_path`` instead would miss half the
    # directory refs: ``_materialize_directory`` re-adds the delimiter itself
    # (``storage_path.rstrip("/") + "/"``), so a directory ref is not guaranteed
    # to carry one.
    local = Path(ref.local_path) if ref.local_path is not None else None

    if remote_meta is None or remote_meta[0] == 0:
        # Nothing under the prefix, and at the exact key either no object at all
        # or only a marker's zero bytes. A ref whose ``local_path`` is a
        # directory is a directory-backed ref over an empty prefix — exactly the shape ``download()`` hands back for a prefix with
        # no objects (``local_path`` a fresh empty temp dir, ``file_count`` 0;
        # see transfer.py "Directory / prefix" branch). It must not reach the
        # single-file branch, which hashes ``local_path`` to decide whether it can
        # skip the download: hashing a directory raises ``IsADirectoryError`` out
        # of ``open(path, "rb")``.
        #
        # Returning the ref unchanged hands back the directory it already names.
        # The SDK does not decide what an empty hand-off MEANS — an empty upstream
        # is legitimate for one caller and a lost hand-off for the next — so the
        # consumer keeps that judgement, and gets a directory to make it with
        # instead of an exception from the storage layer.
        #
        # ``file_count`` is counted off the disk rather than assumed 0:
        # ``_materialize_directory`` does not prune extraneous local files
        # either, so a directory left behind by an earlier pass keeps its
        # contents, and this log must not assert they are not there.
        if local is not None and local.is_dir():
            # conformance: ignore[L018] keys are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
            logger.debug(
                "file_ref.materialize.empty_prefix",
                storage_path=ref.storage_path,
                local_path=ref.local_path,
                file_count=sum(1 for _ in local.iterdir()),
            )
            return ref
        # Nothing local to hand back, so fall through to the single-file
        # branch: with no object at all it raises StorageNotFoundError, whose
        # message names all three causes (the writer has not deposited yet, the
        # path is wrong, or the credentials lack list/read permission here), and
        # with a 0-byte object it materialises those zero bytes as before.

    if ref.local_path is None:
        return await _materialize_single_file(
            store, ref, local_dir, remote_meta=remote_meta
        )
    async with _materialize_guard(ref.local_path):
        return await _materialize_single_file(
            store, ref, local_dir, remote_meta=remote_meta
        )


# Structured kwargs in the logger calls of the two helpers below are intentional: every key used
# (storage_path, local_path, file_size_bytes, bytes_downloaded,
# bytes_transferred_before_failure, sha256, tier, file_count, files_skipped,
# files_downloaded, chunks_total, is_cache_hit, error_type, duration_ms) is
# in _KNOWN_EXTRA_KEYS (logger_adaptor.py "FileReference transfers", lines 106-126).
# _build_extra_dict promotes them to top-level OTLP attributes — indexed columns in
# Grafana+ClickHouse. Do not rewrite to %-style; that would lose the promotion.
async def _materialize_single_file(
    store: ObjectStore,
    ref: FileReference,
    local_dir: str | None,
    *,
    remote_meta: tuple[int, str | None] | None = None,
) -> FileReference:
    """Materialise one durable single-file ref (see ``materialize_file_reference``).

    Callers with a stable ``ref.local_path`` hold the per-path materialise
    lock around this call; the fast-path re-check below is therefore the
    post-wait re-check that lets the second concurrent activity skip the
    duplicate download.

    Args:
        remote_meta: The ``(size, etag)`` HEAD on ``ref.storage_path`` that the
            dispatcher already made to route here, handed down so the same
            round trip is not made twice. ``None`` means "not supplied" and the
            HEAD is made below — the dispatcher never passes ``None``, because
            it only routes here once that HEAD has proved an object exists, so
            ``None`` reaches this only from a direct caller.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
        FILE_REF_CHUNK_CONCURRENCY,
        FILE_REF_CHUNK_SIZE_BYTES,
        FILE_REF_CHUNKED_THRESHOLD_BYTES,
    )
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageNotFoundError,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        download_file,
        download_file_chunked,
        get_file_meta,
    )

    # ── Single file ────────────────────────────────────────────────────
    # Fast path: local file exists — validate before deciding to download.
    #
    # ``is_file()``, not ``exists()``: a directory also exists, and the very
    # next line opens this path to hash it. The caller already routes a
    # directory-backed ref away from here, so this is the second line of
    # defence for a ref that reaches this helper by another path.
    stored_hash: str | None = None
    if ref.local_path is not None and Path(ref.local_path).is_file():
        local_hash = await integrity.sha256_file(Path(ref.local_path))
        stored_hash = await integrity.read_expected_digest(store, ref.storage_path)

        if stored_hash is not None and local_hash == stored_hash:
            # File is intact — stamp local sidecar and reuse.
            _write_local_sidecar(ref.local_path, local_hash)
            # conformance: ignore[L018] keys are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
            logger.debug(
                "file_ref.materialize.skipped",
                storage_path=ref.storage_path,
                local_path=ref.local_path,
                is_cache_hit=True,
            )
            return ref
        # Otherwise (no stored sidecar OR hash mismatch) fall through
        # to re-download — conservative since we cannot verify.

    # Determine output path. owns_temp: a fresh mkstemp name per call
    # means a resume checkpoint could never be reused on retry — disable
    # resume and clean up the partial + sidecar on failure. A stable
    # ref.local_path keeps the default (env-driven) resume behaviour so a
    # Temporal retry on the same pod fetches only the missing ranges.
    owns_temp = ref.local_path is None
    if ref.local_path is not None:
        out_path = ref.local_path
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    else:
        suffix = Path(ref.storage_path).suffix or ""
        if local_dir:
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            fd, out_path = tempfile.mkstemp(suffix=suffix, dir=local_dir)
        else:
            fd, out_path = tempfile.mkstemp(suffix=suffix)
        os.close(
            fd
        )  # close immediately; this only reserves the destination name — the download stages in .sdk-partial/ and publishes over it

    # Use get_file_meta (HEAD) for two purposes: existence check (avoids
    # the ambiguous empty-listing → misleading 404 from download_file) and
    # threshold check for chunked vs streaming download.
    # list_keys() with empty result alone cannot distinguish "single
    # file at this exact key" from "no objects under this prefix":
    # list_keys appends a trailing slash so a real single file always
    # lists empty here, AND some stores (notably GCS with conditional
    # IAM) silently return an empty listing when the caller lacks
    # permission.
    #
    # The dispatcher makes exactly this HEAD to decide it should route here at
    # all, and hands the result down; only a direct caller leaves it unset.
    if remote_meta is None:
        remote_meta = await get_file_meta(ref.storage_path, store, normalize=False)
    remote_size, remote_etag = remote_meta if remote_meta is not None else (None, None)
    if remote_size is None:
        raise StorageNotFoundError(
            f"FileReference path '{ref.storage_path}' resolved to no "
            f"objects under the prefix and no single file at the exact "
            f"key. Either the upstream writer has not deposited files "
            f"yet, the path is wrong, or the store credentials lack "
            f"list/read permission on this location.",
            key=ref.storage_path,
        )

    _is_chunked = remote_size >= FILE_REF_CHUNKED_THRESHOLD_BYTES
    _chunks_total = (
        max(
            1,
            (remote_size + FILE_REF_CHUNK_SIZE_BYTES - 1) // FILE_REF_CHUNK_SIZE_BYTES,
        )
        if _is_chunked
        else 1
    )
    _log = logger.info if remote_size >= _INFO_LOG_THRESHOLD else logger.debug
    _t0 = time.monotonic()
    _log(
        "file_ref.materialize.start",
        storage_path=ref.storage_path,
        file_size_bytes=remote_size,
        is_cache_hit=False,
        tier=str(ref.tier),
    )

    try:
        # Dispatch to chunked (parallel range-GET) or single-stream download.
        if _is_chunked:
            sha256 = await download_file_chunked(
                ref.storage_path,
                out_path,
                store,
                chunk_size_bytes=FILE_REF_CHUNK_SIZE_BYTES,
                max_concurrent_chunks=FILE_REF_CHUNK_CONCURRENCY,
                compute_hash=True,
                normalize=False,
                # remote_size/etag already fetched via get_file_meta (HEAD)
                # above; reuse both so the chunked path doesn't HEAD a
                # second time and its range GETs are version-pinned.
                file_size=remote_size,
                etag=remote_etag,
                resume=False if owns_temp else None,
                # The local-file fast path above may already have fetched
                # the producer's digest; hand it down rather than making the
                # transfer layer re-read the sidecar. Verification itself
                # lives there now, so it fires for every download in the
                # SDK rather than only for FileReference (FND-306).
                expected_sha256=stored_hash,
            )
        else:
            sha256 = await download_file(
                ref.storage_path,
                out_path,
                store,
                compute_hash=True,
                normalize=False,
                expected_sha256=stored_hash,
            )

        if sha256 is None:
            raise StorageNotFoundError(
                f"FileReference storage path not found in store: {ref.storage_path}",
                key=ref.storage_path,
            )

        _write_local_sidecar(out_path, sha256)
    except Exception as exc:
        # conformance: ignore[L018,L009] structured failure event; keys promoted to indexed OTLP attributes via _KNOWN_EXTRA_KEYS; distinct transfer-boundary telemetry not re-emitted by caller
        logger.error(
            "file_ref.materialize.failed",
            storage_path=ref.storage_path,
            error_type=type(exc).__name__,
            bytes_transferred_before_failure=0,
            exc_info=True,
        )
        if owns_temp:
            # A fresh-named temp can never be resumed — don't strand the
            # partial file or its checkpoint sidecar (best-effort).
            try:
                from application_sdk.storage.chunked import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                    _discard_transfer_state,
                )

                Path(out_path).unlink(missing_ok=True)
                # The writer's own cleanup, so this site cannot drift from the
                # staging layout; it leaves the destination alone, which is why
                # that unlink is separate above.
                _discard_transfer_state(Path(out_path))
            except OSError:  # conformance: ignore[E002] best-effort cleanup of an unusable temp; original error re-raised below
                pass
        raise

    _log(
        "file_ref.materialize.complete",
        storage_path=ref.storage_path,
        bytes_downloaded=remote_size,
        duration_ms=int((time.monotonic() - _t0) * 1000),
        sha256=sha256,
        chunks_total=_chunks_total,
        tier=str(ref.tier),
    )
    return FileReference(
        local_path=out_path,
        is_durable=True,
        storage_path=ref.storage_path,
        tier=ref.tier,
    )


async def _materialize_directory(
    store: ObjectStore,
    ref: FileReference,
    data_objects: list[DataObject],
    local_directory: str,
) -> FileReference:
    """Materialise a durable directory-prefix ref (see ``materialize_file_reference``).

    Callers hold the per-path materialise lock on *local_directory*, so two
    activities sharing one directory ref neither duplicate downloads nor
    share an in-flight chunked part file.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
        FILE_REF_CHUNK_CONCURRENCY,
        FILE_REF_CHUNK_SIZE_BYTES,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _safe_join_under,
        download_file_chunked,
    )

    assert ref.storage_path is not None  # dispatcher returns early on bare refs

    data_keys = [obj.key for obj in data_objects]

    _t0 = time.monotonic()
    # conformance: ignore[L018] keys are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
    logger.info(
        "file_ref.materialize.start",
        storage_path=ref.storage_path,
        file_count=len(data_keys),
        is_cache_hit=False,
        tier=str(ref.tier),
    )

    prefix = ref.storage_path.rstrip("/") + "/"

    async def _download_one(obj: DataObject) -> bool:
        """Download one file from the prefix. Returns True if skipped (cache hit)."""
        key = obj.key
        rel = key.removeprefix(prefix)
        # Reject keys whose resolved path escapes local_directory.
        dest_path = _safe_join_under(local_directory, rel)
        dest = str(dest_path)
        dest_sidecar = Path(dest + ".sha256")
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Per-file sidecar fast-path: skip re-download when the local
        # file and its sidecar both exist and their hashes agree.
        # This makes same-pod retries (Temporal heartbeat timeouts,
        # OOM recoveries on the same node) free after the first pass.
        if dest_path.exists() and dest_sidecar.exists():
            try:
                local_hash = await integrity.sha256_file(dest_path)
                if local_hash == dest_sidecar.read_text(encoding="utf-8").strip():
                    # conformance: ignore[L018] keys are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
                    logger.debug(
                        "file_ref.materialize.skipped",
                        storage_path=key,
                        local_path=dest,
                        is_cache_hit=True,
                    )
                    return True
            # conformance: ignore[E004] sidecar integrity probe; failure is benign and logged at debug before falling through to re-download
            except Exception:
                # conformance: ignore[L018] keys are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
                logger.debug(
                    "file_ref.materialize.sidecar_check_failed",
                    storage_path=key,
                    local_path=dest,
                    exc_info=True,
                )
                # fall through to re-download

        # Chunk large files (bounded parallel range GETs, each with its own
        # timeout / retry budget) and stream small ones — passing the
        # listing's size so no per-file HEAD is issued. This is the same
        # reliability the single-file branch already has (BLDX-1513); before
        # this, a multi-hundred-MB file inside a directory ref (e.g. a
        # connection-cache SQLite) streamed in one GET and died on the
        # overall-request timeout.
        sha256 = await download_file_chunked(
            key,
            dest,
            store,
            chunk_size_bytes=FILE_REF_CHUNK_SIZE_BYTES,
            max_concurrent_chunks=FILE_REF_CHUNK_CONCURRENCY,
            compute_hash=True,
            normalize=False,
            file_size=obj.size,
            etag=obj.etag,
            sidecar_present=obj.has_sidecar,
        )
        if sha256 is not None:
            _write_local_sidecar(dest, sha256)
        return False

    try:
        from application_sdk.constants import (  # noqa: PLC0415
            MAX_CONCURRENT_STORAGE_TRANSFERS,
        )
        from application_sdk.storage._concurrency import (  # noqa: PLC0415
            _gather_with_semaphore,
        )

        sem = asyncio.Semaphore(MAX_CONCURRENT_STORAGE_TRANSFERS)
        results = await _gather_with_semaphore(
            [_download_one(obj) for obj in data_objects], sem
        )
        skipped = sum(results)
    except Exception as exc:
        # conformance: ignore[L018,L009] structured failure event; keys promoted to indexed OTLP attributes via _KNOWN_EXTRA_KEYS; distinct transfer-boundary telemetry not re-emitted by caller
        logger.error(
            "file_ref.materialize.failed",
            storage_path=ref.storage_path,
            error_type=type(exc).__name__,
            bytes_transferred_before_failure=0,
            exc_info=True,
        )
        raise

    # conformance: ignore[L018] keys are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
    logger.info(
        "file_ref.materialize.complete",
        storage_path=ref.storage_path,
        file_count=len(data_keys),
        files_skipped=skipped,
        files_downloaded=len(data_keys) - skipped,
        duration_ms=int((time.monotonic() - _t0) * 1000),
        tier=str(ref.tier),
    )
    return FileReference(
        local_path=local_directory,
        is_durable=True,
        storage_path=ref.storage_path,
        file_count=len(data_keys),
        tier=ref.tier,
    )


async def fetch(
    ref: FileReference,
    store: ObjectStore | None = None,
) -> FileReference:
    """Materialize a single durable ``FileReference`` on demand.

    Intended for ``Lazy``-marked fields that were not auto-downloaded before
    the activity ran.  Call this inside the activity body when the file is
    actually needed:

        async def my_task(self, inp: MyInput) -> MyOutput:
            if need_heavy_artifact:
                ref = await fetch(inp.heavy_artifact, store)
                # ref.local_path is now set

    Repeated calls are cheap — ``materialize_file_reference`` checks the
    local SHA-256 sidecar and skips re-downloading if the file is intact.

    Args:
        ref: A durable ``FileReference``.  If not durable, returned as-is.
        store: Object store to download from.  If ``None``, resolved from the
            current activity's infrastructure context — requires the call to
            originate inside an activity.

    Returns:
        A ``FileReference`` with ``local_path`` set to the downloaded file.

    Raises:
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is
            available (i.e. called outside an activity without an explicit store).
    """
    if not ref.is_durable:
        return ref

    if store is None:
        from application_sdk.infrastructure.context import (  # noqa: PLC0415 — deferred: infrastructure context is only available at runtime inside an activity
            get_infrastructure,
        )

        infra = get_infrastructure()
        if infra is None or infra.storage is None:
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                ObjectStoreNotProvidedError,
            )

            raise ObjectStoreNotProvidedError()
        store = infra.storage

    return await materialize_file_reference(store, ref)
