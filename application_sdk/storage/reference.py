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
import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from application_sdk.common._listing import safe_list_directory
from application_sdk.contracts.types import FileReference
from application_sdk.execution.heartbeat import run_in_thread

if TYPE_CHECKING:
    from obstore.store import ObjectStore

from application_sdk.observability.logger_adaptor import get_logger

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


def _sha256_hex_file(path: Path) -> str:
    """Return the hex-encoded SHA-256 digest of *path* using chunked reads.

    Reads in 1 MiB chunks so memory usage is constant regardless of file size.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _sha256_hex_file_async(path: Path) -> str:
    """Run :func:`_sha256_hex_file` in a worker thread.

    ``_sha256_hex_file`` reads and digests the whole file with no ``await``;
    calling it directly on the event loop blocks the loop for the full
    read+hash. A blocked loop cannot run the SDK's auto-heartbeat coroutine, so
    activities that verify many/large files heartbeat-time-out even while making
    progress. Uses ``run_in_thread`` (dedicated pool) rather than
    ``asyncio.to_thread`` (asyncio's default executor) so this doesn't
    contend with Temporal's own use of the default executor — same reason
    directory listing is offloaded this way in ``persist``.
    """
    return await run_in_thread(_sha256_hex_file, path)


async def _get_stored_sidecar(storage_path: str, store: ObjectStore) -> str | None:
    """Fetch the stored sha256 sidecar for *storage_path*, or None if absent.

    Uses a HEAD request first to confirm the sidecar exists before issuing a
    GET.  This avoids the obstore Rust retry cycle (up to the configured
    retry_timeout) that would otherwise fire on every missing sidecar — which
    is common for refs persisted before sidecar support was added.
    """
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _get_bytes,
        exists,
    )

    sidecar_key = storage_path + ".sha256"
    try:
        if not await exists(sidecar_key, store, normalize=False):
            return None
        raw = await _get_bytes(sidecar_key, store, normalize=False)
        return raw.decode().strip() if raw else None
    except Exception:
        logger.warning("Failed to fetch sha256 sidecar from store", exc_info=True)
        return None


def _write_local_sidecar(local_path: str, sha256: str) -> None:
    """Write a local ``.sha256`` sidecar next to *local_path*."""
    try:
        Path(local_path + ".sha256").write_text(sha256)
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
        _put,
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
            sha256 = await upload_file(file_key, file_path, store, normalize=False)
            assert sha256 is not None
            try:
                await _put(
                    file_key + ".sha256", sha256.encode(), store, normalize=False
                )
            except Exception:
                logger.warning(
                    "Sidecar write failed (best-effort, continuing without)",
                    exc_info=True,
                )

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
            sha256 = await upload_file(storage_path, local, store, normalize=False)
            # compute_hash defaults to True, so the digest is always returned here.
            assert sha256 is not None
            try:
                await _put(
                    storage_path + ".sha256", sha256.encode(), store, normalize=False
                )
            except Exception:
                logger.warning(
                    "Sidecar write failed (best-effort, continuing without)",
                    exc_info=True,
                )
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

    Uses ``list_keys`` to determine whether *ref* is a single file or a
    directory prefix, then downloads accordingly.

    **Single file**: if ``ref.local_path`` already exists on disk AND the
    stored sha256 sidecar confirms the file is intact, the local sidecar is
    (re-)written and the function returns without downloading.

    **Directory**: fast path is always skipped; all files under the prefix
    are re-listed and downloaded.

    Args:
        store: Source obstore store.
        ref: A durable ``FileReference`` with ``storage_path`` set.
        local_dir: Optional directory for temp files/dirs (uses the system
            temp dir if ``None``).

    Returns:
        A ``FileReference`` with ``local_path`` pointing to the verified
        file or directory on the local filesystem.

    Raises:
        StorageNotFoundError: If the key does not exist in the store.
        StorageError: If the downloaded data does not match the stored sidecar.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — circular: storage modules are imported transitively across the SDK
        FILE_REF_CHUNK_CONCURRENCY,
        FILE_REF_CHUNK_SIZE_BYTES,
        FILE_REF_CHUNKED_THRESHOLD_BYTES,
    )
    from application_sdk.storage.batch import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        list_keys_with_meta,
    )
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageError,
        StorageNotFoundError,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _safe_join_under,
        download_file,
        download_file_chunked,
        get_file_meta,
    )

    if not ref.is_durable or ref.storage_path is None:
        return ref  # nothing to materialise

    # Determine single-file vs directory by listing sub-keys under the path.
    # Sizes come back with the listing so the directory branch can chunk large
    # files without a per-file HEAD (BLDX-1513).
    all_items = await list_keys_with_meta(ref.storage_path, store)
    data_items = [(k, s, e) for k, s, e in all_items if not k.endswith(".sha256")]
    data_keys = [k for k, _, _ in data_items]

    # Structured kwargs in the logger calls below are intentional: every key used
    # (storage_path, local_path, file_size_bytes, bytes_downloaded,
    # bytes_transferred_before_failure, sha256, tier, file_count, files_skipped,
    # files_downloaded, chunks_total, is_cache_hit, error_type, duration_ms) is
    # in _KNOWN_EXTRA_KEYS (logger_adaptor.py "FileReference transfers", lines 106-126).
    # _build_extra_dict promotes them to top-level OTLP attributes — indexed columns in
    # Grafana+ClickHouse. Do not rewrite to %-style; that would lose the promotion.
    if not data_keys:
        # ── Single file ────────────────────────────────────────────────────

        # Fast path: local file exists — validate before deciding to download.
        stored_hash: str | None = None
        if ref.local_path is not None and Path(ref.local_path).exists():
            local_hash = await _sha256_hex_file_async(Path(ref.local_path))
            stored_hash = await _get_stored_sidecar(ref.storage_path, store)

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
            os.close(fd)  # close immediately; download_file will overwrite

        # Use get_file_size (HEAD) for two purposes: existence check (avoids
        # the ambiguous empty-listing → misleading 404 from download_file) and
        # threshold check for chunked vs streaming download.
        # list_keys() with empty result alone cannot distinguish "single
        # file at this exact key" from "no objects under this prefix":
        # list_keys appends a trailing slash so a real single file always
        # lists empty here, AND some stores (notably GCS with conditional
        # IAM) silently return an empty listing when the caller lacks
        # permission.
        remote_meta = await get_file_meta(ref.storage_path, store, normalize=False)
        remote_size, remote_etag = (
            remote_meta if remote_meta is not None else (None, None)
        )
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
                (remote_size + FILE_REF_CHUNK_SIZE_BYTES - 1)
                // FILE_REF_CHUNK_SIZE_BYTES,
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
                )
            else:
                sha256 = await download_file(
                    ref.storage_path,
                    out_path,
                    store,
                    compute_hash=True,
                    normalize=False,
                )

            if sha256 is None:
                raise StorageNotFoundError(
                    f"FileReference storage path not found in store: {ref.storage_path}",
                    key=ref.storage_path,
                )

            # Verify against stored sidecar (reuse fetched value if already retrieved).
            if stored_hash is None:
                stored_hash = await _get_stored_sidecar(ref.storage_path, store)
            if stored_hash is not None and sha256 != stored_hash:
                raise StorageError(
                    f"SHA-256 mismatch for {ref.storage_path}: "
                    f"downloaded={sha256}, stored={stored_hash}",
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
                    Path(out_path).unlink(missing_ok=True)
                    Path(str(out_path) + ".transfer-state").unlink(missing_ok=True)
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

    else:
        # ── Directory / prefix ─────────────────────────────────────────────

        if ref.local_path is not None:
            local_directory = ref.local_path
        elif local_dir is not None:
            local_directory = local_dir
        else:
            local_directory = tempfile.mkdtemp()

        Path(local_directory).mkdir(parents=True, exist_ok=True)

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

        async def _download_one(key: str, size: int, etag: str | None) -> bool:
            """Download one file from the prefix. Returns True if skipped (cache hit)."""
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
                    local_hash = await _sha256_hex_file_async(dest_path)
                    if local_hash == dest_sidecar.read_text().strip():
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
                file_size=size,
                etag=etag,
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
                [_download_one(k, s, e) for k, s, e in data_items], sem
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
