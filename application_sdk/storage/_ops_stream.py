"""The streaming halves of :func:`~application_sdk.storage.ops.upload_file` and
:func:`~application_sdk.storage.ops.download_file`.

``ops`` keeps the public functions, their key normalisation, store resolution
and integrity checks; this module holds the byte-moving loops under them:
the multipart upload with its gateway retry, and the download's write into a
staging file that is published with ``os.replace``.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import obstore

from application_sdk._runtime.offload import run_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.common._listing import PARTIAL_DIRNAME
from application_sdk.common.atomic import disk_full_guard, ensure_free_space
from application_sdk.storage import _settings
from application_sdk.storage._budget import transfer_budget
from application_sdk.storage._ops_errors import (
    _exc_class_name,
    _is_not_found,
    _storage_error_for,
)
from application_sdk.storage._ops_seam import (
    _log_storage_event,
    _past_gateway_no_route,
    logger,
)
from application_sdk.storage._telemetry import _log_transfer_progress

if TYPE_CHECKING:
    from obstore import GetResult
    from obstore.store import ObjectStore


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


@contextlib.contextmanager
def _open_chunks(
    source: Path | bytes, size: int
) -> Iterator[Iterator[bytes | memoryview]]:
    """Yield an iterator over *source* in *size*-byte chunks.

    A file is read a chunk at a time; an in-memory payload is sliced through a
    ``memoryview``, so no chunk is copied.
    """
    if isinstance(source, Path):
        with source.open("rb") as fh:
            yield iter(lambda: fh.read(size), b"")
    else:
        view = memoryview(source)
        yield (view[i : i + size] for i in range(0, len(view), size))


async def _upload_once(
    resolved: ObjectStore,
    key: str,
    source: Path | bytes,
    *,
    effective_chunk: int,
    put_attributes: dict[str, str] | None,
    compute_hash: bool,
    started: float,
    file_size: int,
) -> tuple[hashlib._Hash | None, int]:
    """Run one upload attempt; return ``(hasher, bytes_sent)``.

    *source* (a file, or bytes held in memory) is read from the start every
    call, so the hash and byte count belong to this attempt alone.
    """
    h = hashlib.sha256() if compute_hash else None
    _progress_interval = _settings.progress_log_interval_seconds()
    upload_max_concurrency = _settings.upload_max_concurrency()

    last_progress = started
    bytes_sent = 0
    # The writer's parts upload inside obstore, where their completion is not
    # observable, so the reservation covers the writer's whole life: up to
    # max_concurrency parts in flight, the part being filled, and the chunk
    # read here — or twice the file, when it fits in one buffer (FND-4082).
    buffered_bytes = min(2 * file_size, effective_chunk * (upload_max_concurrency + 2))
    async with transfer_budget().reserve(buffered_bytes):
        async with obstore.open_writer_async(
            resolved,
            key,
            buffer_size=effective_chunk,
            max_concurrency=upload_max_concurrency,
            attributes=put_attributes,
        ) as writer:
            with _open_chunks(source, effective_chunk) as chunks:
                for chunk in chunks:
                    if h is not None:
                        h.update(chunk)
                    await writer.write(chunk)
                    bytes_sent += len(chunk)
                    # One multipart part on its way to the store is one
                    # observable unit (ADR-0018). Marking per part rather than
                    # per file is what keeps a single multi-GB upload — the
                    # common shape for a large connector's parquet output —
                    # from looking like one long quiet window to the stall
                    # watchdog. Unconditional, unlike the progress *log* below:
                    # a mark is two stores under an uncontended lock, so it
                    # needs no interval gate.
                    current_progress_tracker().mark_progress("storage.upload_part")
                    if _progress_interval > 0:
                        now = time.monotonic()
                        if now - last_progress >= _progress_interval:
                            _log_transfer_progress(
                                "upload",
                                key,
                                bytes_so_far=bytes_sent,
                                elapsed_ms=(now - started) * 1000.0,
                                total_bytes=file_size,
                            )
                            last_progress = now
    return h, bytes_sent


async def _stream_upload(
    key: str,
    source: Path | bytes,
    resolved: ObjectStore,
    put_attributes: dict[str, str] | None,
    *,
    file_size: int,
    chunk_size: int | None,
    compute_hash: bool,
) -> tuple[str | None, int]:
    """Stream *source* to *key*; return ``(digest, bytes_sent)``.

    The transport shared by :func:`upload_file` (a file) and
    :func:`upload_bytes` (bytes in memory): part sizing and the deployment
    override, the transfer-budget reservation, per-part progress marks, the
    gateway no-route retry, storage telemetry and error translation. The
    integrity checks after it are the caller's.
    """
    part_size_bytes = _settings.upload_part_size_bytes()
    part_size_overridden = _settings.upload_part_size_overridden()

    # A deployment that sets the part size overrides the caller. The size that
    # keeps a completion inside the destination's idle timeout depends on where
    # the deployment writes; an app cannot know which tenant it lands on, so it
    # must not be able to pin a value the operator then cannot correct.
    if (
        part_size_overridden
        and chunk_size is not None
        and chunk_size != part_size_bytes
    ):
        logger.debug(
            "Using deployment part size %d instead of the requested %d for key '%s'",
            part_size_bytes,
            chunk_size,
            key,
        )
    requested_chunk = (
        part_size_bytes if (part_size_overridden or chunk_size is None) else chunk_size
    )

    effective_chunk = _compute_part_size(file_size, requested_chunk)

    if file_size == 0:
        logger.warning(
            "Uploading zero-byte object to key '%s' — "
            "some S3-style backends silently drop empty objects; "
            "verify the object exists after upload if your store requires it.",
            key,
        )

    # Each attempt re-reads the file from the start, so the hash, byte count and
    # clock restart with it; only the gateway's transient no-route 404 retries.
    started = time.monotonic()

    async def _attempt() -> tuple[hashlib._Hash | None, int]:
        nonlocal started
        started = time.monotonic()
        return await _upload_once(
            resolved,
            key,
            source,
            effective_chunk=effective_chunk,
            put_attributes=put_attributes,
            compute_hash=compute_hash,
            started=started,
            file_size=file_size,
        )

    try:
        h, bytes_sent = await _past_gateway_no_route("upload", key, _attempt)
    # conformance: ignore[E004] upload error handler; _log_storage_event records error_class and exception is re-raised via StorageError chain
    except BaseException as exc:
        # BaseException is the umbrella for Exception and its siblings
        # (CancelledError, KeyboardInterrupt, SystemExit). Catching it here
        # ensures cancellation mid-writer-close is logged rather than silently
        # discarding the buffer and leaving no object in the store. A retried
        # gateway 404 never reaches here: the upload has not failed yet.
        elapsed_ms = (time.monotonic() - started) * 1000.0
        from application_sdk.errors.base import (  # noqa: PLC0415 — lazy: avoid import-time cycle errors<->storage
            redact_secrets,
        )

        _log_storage_event(
            logging.WARNING,
            "upload",
            key,
            outcome="failure",
            elapsed_ms=elapsed_ms,
            size_bytes=file_size,
            error_class=_exc_class_name(exc),
            error_message=redact_secrets(str(exc)),
        )
        if isinstance(exc, Exception):
            raise _storage_error_for(
                exc, key, f"Failed to upload file to key '{key}'", resolved
            ) from exc
        raise  # re-raise CancelledError / KeyboardInterrupt bare after logging

    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log_storage_event(
        logging.DEBUG,
        "upload",
        key,
        outcome="success",
        elapsed_ms=elapsed_ms,
        size_bytes=bytes_sent,
    )
    digest = h.hexdigest() if h is not None else None
    return digest, bytes_sent


async def _write_download(
    result: GetResult,
    path: Path,
    key: str,
    local_path: str | Path,
    *,
    digest: hashlib._Hash | None,
    min_chunk_size: int,
    started: float,
) -> int:
    """Stream a GET *result* into a staging file and publish it at *path*.

    The write half of :func:`~application_sdk.storage.ops.download_file`
    (CONNECT-1126): the bytes land in a ``mkstemp`` file inside the
    ``.sdk-partial`` directory beside *path* and reach it through
    ``os.replace``, so the destination only ever holds complete content. The
    staging file is removed on any failure, cancellation included.

    Returns:
        The number of bytes written. *digest*, when given, is updated with
        every chunk.

    Raises:
        StorageDiskFullError: The volume could not hold the object.
        StorageError: The write failed for any other reason.
    """
    h = digest
    bytes_written = 0
    tmp_name: str | None = None
    published = False
    try:
        try:
            # A full volume is its own failure, not a generic storage one: it
            # names the volume to resize, where StorageError reads as the store.
            # The declared size is known before the first byte arrives, so a
            # volume that plainly cannot hold the object fails here, in seconds,
            # and the message can say what was needed.
            declared = result.meta["size"]
            ensure_free_space(path, declared, operation="download")
            with disk_full_guard(path, operation="download", required_bytes=declared):
                # Inside the guard: a missing parent needs room for its own
                # directory entry, and a full volume there is the same failure.
                path.parent.mkdir(parents=True, exist_ok=True)
                staging_dir = path.parent / PARTIAL_DIRNAME
                # mode hardens only the first creation (ignored when the directory
                # exists) — it keeps a staging dir under a shared temp root private.
                os.makedirs(staging_dir, mode=0o700, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(staging_dir), prefix=path.name + "."
                )
                _progress_interval = _settings.progress_log_interval_seconds()

                last_progress = started
                # The stream holds up to one min_chunk_size buffer on the Rust
                # side and its copy as Python bytes; the budget bounds that
                # across every concurrent transfer (FND-4082).
                stream_bytes = 2 * min(declared, min_chunk_size)
                with os.fdopen(fd, "wb") as fh:
                    async with transfer_budget().reserve(stream_bytes):
                        async for chunk in result.stream(min_chunk_size=min_chunk_size):
                            raw = bytes(chunk)
                            fh.write(raw)
                            bytes_written += len(raw)
                            if h is not None:
                                h.update(raw)
                            # One streamed chunk landed on disk — see the
                            # matching mark in upload_file for why this is per
                            # chunk and ungated.
                            current_progress_tracker().mark_progress(
                                "storage.download_chunk"
                            )
                            if _progress_interval > 0:
                                now = time.monotonic()
                                if now - last_progress >= _progress_interval:
                                    _log_transfer_progress(
                                        "download",
                                        key,
                                        bytes_so_far=bytes_written,
                                        elapsed_ms=(now - started) * 1000.0,
                                    )
                                    last_progress = now
                    # fsync before the publish: on a delayed-allocation filesystem
                    # ENOSPC can surface only at writeback, and without this a
                    # short file would be published as complete (the FND-318
                    # argument). Offloaded so a large flush does not hold the
                    # event loop and the activity heartbeat with it.
                    fh.flush()
                    await run_in_thread(os.fsync, fh.fileno())
                os.replace(tmp_name, path)
                published = True
        # conformance: ignore[E004] file-write error handler; _log_storage_event records error_class and the exception is re-raised as StorageDiskFullError or StorageError
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
            from application_sdk.errors import DiskFullError  # noqa: PLC0415

            if isinstance(exc, DiskFullError):
                # Also a StorageError, so `except StorageError:` and the
                # fan-out unwrapping in `_run_bounded` still see it.
                from application_sdk.storage.errors import (  # noqa: PLC0415
                    StorageDiskFullError,
                )

                raise StorageDiskFullError.from_disk_full(exc, key=key) from (
                    exc.__cause__ or exc
                )
            from application_sdk.storage.errors import StorageError  # noqa: PLC0415

            raise StorageError(
                f"Failed to write downloaded file to '{local_path}'", key=key, cause=exc
            ) from exc
    finally:
        # BaseException-safe: a Temporal cancel or worker shutdown must not
        # strand a uniquely-named staging file per attempt.
        #
        # Suppressed, like common.atomic._discard and the chunked path's
        # _discard_transfer_state: `missing_ok` covers only FileNotFoundError,
        # and a raise out of this `finally` would replace whatever the caller
        # was about to receive — a completed download, or the typed
        # StorageError above — with a bare OSError about the temp file.
        if not published and tmp_name is not None:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)
    return bytes_written


async def _open_download(resolved: ObjectStore, key: str, started: float) -> GetResult:
    """Send the download's GET through the request seam; type its failure.

    The request half of :func:`~application_sdk.storage.ops.download_file`. A
    failure records its per-attempt storage event and raises the typed error:
    :class:`~application_sdk.storage.errors.StorageNotFoundError` for a
    missing key, the :func:`_storage_error_for` leaf for anything else.
    """
    try:
        result = await _past_gateway_no_route(
            "download", key, lambda: obstore.get_async(resolved, key)
        )
    # conformance: ignore[E004] download error handler; _log_storage_event records error_class and exception is re-raised via StorageError chain
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
        raise _storage_error_for(
            exc, key, f"Failed to download key '{key}'", resolved
        ) from exc
    return result
