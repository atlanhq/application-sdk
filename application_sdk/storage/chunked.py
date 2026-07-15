"""Chunked, resumable, version-pinned downloads (BLDX-1513 / BLDX-1523).

Split out of ``storage/ops.py``: this module owns the parallel range-GET
download path and its resume-checkpoint sidecar, leaving ``ops`` to
single-request I/O primitives. ``download_file_chunked`` remains importable
from both ``application_sdk.storage`` and ``application_sdk.storage.ops``
(back-compat re-export).

Resumable chunked-download state
--------------------------------
A chunked download writes ranges at fixed offsets into a pre-allocated file.
The sidecar records which chunk indices have landed on disk, plus the identity
of the remote object they came from (key / size / chunk size / etag). Its
*existence* is the "incomplete" marker: it is deleted on success, so a data
file without a state sidecar is always complete. On retry after a crash the
download resumes by fetching only the missing chunk indices — valid only while
the remote object is unchanged, which is what the etag match enforces.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import obstore
import orjson

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage._telemetry import _log_transfer_progress

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.storage.ops import BoundStore

logger = get_logger(__name__)

_TRANSFER_STATE_SUFFIX = ".transfer-state"


def _transfer_state_path(path: Path) -> Path:
    """Sidecar path holding resumable-download state for *path*."""
    return Path(str(path) + _TRANSFER_STATE_SUFFIX)


def _load_transfer_state(state_path: Path) -> dict | None:
    """Load and validate a transfer-state sidecar; ``None`` if absent/corrupt.

    Corrupt or structurally invalid state must never break a download — the
    caller falls back to a fresh full download, which is always correct.
    """
    try:
        raw = state_path.read_bytes()
        state = orjson.loads(raw)
        if not isinstance(state, dict):
            return None
        if not isinstance(state.get("key"), str):
            return None
        if not isinstance(state.get("file_size"), int):
            return None
        if not isinstance(state.get("chunk_size"), int):
            return None
        if not isinstance(state.get("done"), list) or not all(
            isinstance(i, int) for i in state["done"]
        ):
            return None
        return state
    # conformance: ignore[E004] absent/corrupt checkpoint is an expected condition; falling back to a fresh download is always correct
    except Exception:
        return None


def _save_transfer_state(state_path: Path, state: dict) -> None:
    """Atomically persist transfer state (write temp + rename, fsync'd).

    fsync on the temp file is enough for the failure mode resume targets:
    process death (OOMKill / activity retry) preserves the kernel page cache,
    so data-file writes that precede this checkpoint are visible on retry.
    Node death loses the local volume entirely — resume is moot there and the
    etag check makes the resulting fresh download safe.
    """
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    # O_BINARY: raw os.write on a Windows text-mode fd would rewrite 0x0A as
    # 0x0D 0x0A (no-op flag on POSIX, where the attribute doesn't exist).
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        os.write(fd, orjson.dumps(state))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, state_path)


def _discard_transfer_state(path: Path) -> None:
    """Remove the data file and its state sidecar (best-effort)."""
    path.unlink(missing_ok=True)
    _transfer_state_path(path).unlink(missing_ok=True)


def _load_and_validate_checkpoint(
    state_path: Path,
    *,
    key: str,
    size: int,
    chunk_size_bytes: int,
    etag: str | None,
    path: Path,
) -> set[int]:
    """Return the completed chunk indices from a trustworthy checkpoint.

    A checkpoint is honoured only when it describes exactly this object
    generation (key / size / chunk size / etag all match) AND the partial data
    file is still the pre-allocated size. Anything else — absent, corrupt,
    structurally invalid, or from another generation — deletes the sidecar and
    returns an empty set (fresh download).
    """
    st = _load_transfer_state(state_path)
    if (
        st is not None
        and st.get("key") == key
        and st.get("file_size") == size
        and st.get("chunk_size") == chunk_size_bytes
        and st.get("etag") == etag
        and path.is_file()
        and path.stat().st_size == size
    ):
        return set(st["done"])
    state_path.unlink(missing_ok=True)
    return set()


@dataclass
class _ChunkProgress:
    """Shared per-attempt counters mutated by the chunk workers.

    Read-modify-write of these fields is race-free because asyncio is
    single-threaded and the workers never ``await`` between an update and the
    next chunk's update.
    """

    started: float
    last_progress: float
    #: Bytes on disk including a previous attempt's chunks (truthful %).
    completed_bytes: int = 0
    #: Bytes THIS attempt transferred — what the terminal event/metrics report.
    fetched_bytes: int = 0
    done: set[int] = field(default_factory=set)


async def _fetch_chunk(
    store: ObjectStore,
    key: str,
    *,
    idx: int,
    offset: int,
    size: int,
    chunk_size_bytes: int,
    etag: str | None,
    fd: int,
    sem: asyncio.Semaphore,
    resume: bool,
    state_path: Path,
    state_base: dict,
    progress: _ChunkProgress,
    progress_interval: float,
) -> None:
    """Fetch one range and write it at its fixed offset; checkpoint + heartbeat."""
    length = min(chunk_size_bytes, size - offset)
    async with sem:
        if etag is not None:
            # Version-pinned range GET: 412 (PreconditionError) if the
            # object was rewritten — never mixes two generations.
            result = await obstore.get_async(
                store,
                key,
                options={
                    "range": (offset, offset + length),
                    "if_match": etag,
                },
            )
            raw = bytes(await result.bytes_async())
        else:
            raw = bytes(
                await obstore.get_range_async(store, key, start=offset, length=length)
            )
        # lseek+write instead of pwrite (Windows lacks pwrite). Safe only
        # because asyncio is single-threaded: no await between the two
        # calls means no other coroutine can interleave on the fd position.
        # WARNING: if _fetch_chunk is ever moved into a thread (e.g. via
        # asyncio.to_thread), lseek+write becomes a data race — two threads
        # could interleave their seeks and corrupt each other's writes.
        # Use os.pwrite (or a per-thread fd) instead if that happens.
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, raw)
        progress.done.add(idx)
        if resume:
            _save_transfer_state(
                state_path, {**state_base, "done": sorted(progress.done)}
            )
        progress.completed_bytes += len(raw)
        progress.fetched_bytes += len(raw)
        if progress_interval > 0:
            now = time.monotonic()
            if now - progress.last_progress >= progress_interval:
                _log_transfer_progress(
                    "download",
                    key,
                    bytes_so_far=progress.completed_bytes,
                    elapsed_ms=(now - progress.started) * 1000.0,
                    total_bytes=size,
                )
                progress.last_progress = now


def _handle_chunk_failure(
    exc: Exception,
    *,
    key: str,
    path: Path,
    resume: bool,
    attempt: int,
) -> bool:
    """Classify a chunk failure: return ``True`` to restart fresh, else raise.

    * First 412 (object rewritten mid-download): discard the mixed-generation
      partial and signal the caller to restart fresh once.
    * Second 412 / not-found: discard and raise typed errors.
    * Anything else: with resume enabled the partial + sidecar stay on disk
      for the next attempt; legacy (no-resume) deletes the partial.
    """
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: ops re-exports download_file_chunked from this module
        _is_not_found,
        _is_precondition,
    )

    if _is_precondition(exc):
        # Object rewritten mid-download: the partial file mixes
        # generations — discard it and restart fresh ONCE against
        # whatever is now in the store.
        _discard_transfer_state(path)
        if attempt == 1:
            logger.warning(
                "Object changed during chunked download; restarting fresh: %s",
                key,
                exc_info=True,
            )
            return True
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(
            f"Object at '{key}' kept changing during chunked download "
            f"(etag precondition failed twice)",
            key=key,
            cause=exc,
        ) from exc
    if _is_not_found(exc):
        _discard_transfer_state(path)
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageNotFoundError,
        )

        raise StorageNotFoundError(
            f"Key not found during chunked download: {key}", key=key
        ) from exc
    if not resume:
        # Legacy behaviour: no checkpoint, so a partial file is garbage.
        path.unlink(missing_ok=True)
    # With resume enabled the partial file + sidecar stay on disk —
    # the next attempt (Temporal retry, same pod) fetches only the
    # missing ranges recorded in the checkpoint.
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageError,
    )

    raise StorageError(
        f"Chunked download failed for '{key}'", key=key, cause=exc
    ) from exc


async def download_file_chunked(
    key: str,
    local_path: str | Path,
    store: BoundStore | ObjectStore | None = None,
    *,
    chunk_size_bytes: int = 16 * 1024 * 1024,
    max_concurrent_chunks: int = 4,
    compute_hash: bool = False,
    normalize: bool = True,
    file_size: int | None = None,
    etag: str | None = None,
    resume: bool | None = None,
) -> str | None:
    """Download *key* using parallel range GETs, writing chunks at fixed offsets.

    For files larger than *chunk_size_bytes*, issues multiple independent
    range requests (up to *max_concurrent_chunks* in flight at once) and
    writes each chunk to the correct file offset via ``os.lseek`` +
    ``os.write`` (``os.pwrite`` is unavailable on Windows).
    Each chunk gets its own obstore retry budget, so a mid-stream stall only
    retries the affected chunk — not the entire file.

    Falls through to :func:`~application_sdk.storage.ops.download_file`
    (single streaming GET) when the remote object is smaller than
    *chunk_size_bytes*.

    **Version pinning (BLDX-1523):** when *etag* is known (supplied by the
    caller from a listing, or captured by the internal HEAD), every range GET
    carries ``If-Match: etag``. If the remote object is rewritten mid-download
    the store answers 412 instead of serving bytes from the new version, so
    chunks can never mix two object generations. On a 412 the partial file is
    discarded and the download restarts fresh **once** against the new
    generation; a second 412 raises.

    **Resume (BLDX-1523):** when *resume* is enabled, completed chunk indices
    are checkpointed to a ``{local_path}.transfer-state`` sidecar after every
    chunk write, and an interrupted download leaves the partial file + sidecar
    on disk instead of deleting them. A retry that resolves the same object
    generation (key / size / chunk size / etag all match) re-fetches only the
    missing chunks. The sidecar is deleted on success, so a data file without
    one is always complete. Resume requires the retry to see the same local
    filesystem (same pod or persistent volume); after node loss the download
    simply starts fresh.

    Args:
        key: Source object key.  Normalised by default.
        local_path: Destination path (created / overwritten).
        store: Source store, or ``None`` to use the infrastructure store.
        chunk_size_bytes: Size of each range-GET chunk (default 16 MiB).
        max_concurrent_chunks: Maximum number of in-flight chunk requests
            (default 4).
        compute_hash: When ``True``, compute and return a SHA-256 digest over
            the completed file. Default ``False`` (matches
            :func:`~application_sdk.storage.ops.download_file`) — hashing
            re-reads the whole file, so callers opt in only when they need
            the digest.
        normalize: When ``True`` (default), normalise *key* before use.
        file_size: Pre-known object size in bytes. When supplied (e.g. from a
            prior listing that already carried sizes), the internal HEAD is
            skipped — this avoids a per-file HEAD when fanning out over a prefix
            whose sizes are already known. When ``None`` (default) a HEAD is
            issued, which also serves as the existence check.
        etag: Pre-known object etag (pairs with *file_size* from the same
            listing). When ``None`` and a HEAD is issued anyway, the etag is
            captured from the HEAD. When ``None`` and *file_size* is supplied,
            range GETs are unpinned — same semantics as before BLDX-1523.
        resume: Resume an interrupted download from its checkpoint sidecar.
            ``None`` (default) follows ``ATLAN_STORAGE_RESUME_DOWNLOADS``
            (enabled unless set to ``"false"``).

    Returns:
        Hex-encoded SHA-256 digest if *compute_hash* is ``True``, else ``None``.

    Raises:
        StorageNotFoundError: If *key* does not exist.
        StorageError: If a chunk download or the disk write fails, or the
            object was rewritten during both the original and restarted attempt.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: ops re-exports download_file_chunked for back-compat
        _exc_class_name,
        _is_not_found,
        _log_storage_event,
        _resolve_store,
        download_file,
        normalize_key,
    )

    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if resume is None:
        from application_sdk.constants import STORAGE_RESUME_DOWNLOADS  # noqa: PLC0415

        resume = STORAGE_RESUME_DOWNLOADS

    from application_sdk.constants import (  # noqa: PLC0415
        STORAGE_PROGRESS_LOG_INTERVAL_SECONDS as _progress_interval,
    )

    attempt = 0
    while True:
        attempt += 1

        # HEAD to get exact size before allocating; also serves as the existence
        # check. Skipped when the caller already knows the size (file_size), e.g.
        # a prefix download whose listing carried per-object sizes (+ etags).
        if file_size is None:
            try:
                meta = await obstore.head_async(resolved, key)
                file_size = int(meta["size"])
                if etag is None:
                    etag = meta.get("e_tag")
            # conformance: ignore[E004] not-found and errors both re-raised via StorageError chain; no silent swallow
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

                raise StorageError(
                    f"Failed to head key '{key}'", key=key, cause=exc
                ) from exc

        # Pin the resolved size for this attempt: `file_size` stays optional
        # across restarts (reset to None to force a re-HEAD), `size` is the
        # narrowed int used everywhere below, including the chunk workers.
        assert file_size is not None
        size: int = file_size
        state_path = _transfer_state_path(path)

        # Small files: delegate to the single-stream path so they still use the
        # streaming GET (avoids materialising the whole body via range GETs).
        # Drop any stale checkpoint from an earlier, larger object generation.
        if size <= chunk_size_bytes:
            state_path.unlink(missing_ok=True)
            return await download_file(
                key, local_path, resolved, compute_hash=compute_hash, normalize=False
            )

        done: set[int] = (
            _load_and_validate_checkpoint(
                state_path,
                key=key,
                size=size,
                chunk_size_bytes=chunk_size_bytes,
                etag=etag,
                path=path,
            )
            if resume
            else set()
        )
        resuming = bool(done)

        # Pre-allocate the file at the target size so lseek can address any
        # offset. On resume, open WITHOUT O_TRUNC so completed chunks survive.
        # 0o600: owner-only — downloaded artifacts can contain extracted customer
        # metadata; don't rely on the process umask to keep them private.
        # O_BINARY: raw os.write on a Windows text-mode fd would rewrite 0x0A
        # as 0x0D 0x0A, corrupting content and shifting every later chunk's
        # offset (no-op flag on POSIX, where the attribute doesn't exist).
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | (0 if resuming else os.O_TRUNC)
            | getattr(os, "O_BINARY", 0)
        )
        fd = os.open(str(path), flags, 0o600)
        try:
            os.ftruncate(fd, size)
        # conformance: ignore[E004] cleanup-on-error guard; closes fd then re-raises immediately with no swallow
        except Exception:
            os.close(fd)
            raise

        offsets = list(range(0, size, chunk_size_bytes))
        pending = [(i, off) for i, off in enumerate(offsets) if i not in done]
        state_base = {
            "key": key,
            "file_size": size,
            "chunk_size": chunk_size_bytes,
            "etag": etag,
        }

        sem = asyncio.Semaphore(max_concurrent_chunks)
        started = time.monotonic()
        progress = _ChunkProgress(
            started=started,
            last_progress=started,
            # Bytes already on disk from a previous attempt count toward
            # progress so the % is truthful.
            completed_bytes=sum(min(chunk_size_bytes, size - offsets[i]) for i in done),
            done=done,
        )

        if resuming:
            _log_storage_event(
                logging.INFO,
                "download",
                key,
                outcome="resume",
                size_bytes=progress.completed_bytes,
            )

        chunk_tasks = [
            asyncio.ensure_future(
                _fetch_chunk(
                    resolved,
                    key,
                    idx=i,
                    offset=off,
                    size=size,
                    chunk_size_bytes=chunk_size_bytes,
                    etag=etag,
                    fd=fd,
                    sem=sem,
                    resume=resume,
                    state_path=state_path,
                    state_base=state_base,
                    progress=progress,
                    progress_interval=_progress_interval,
                )
            )
            for i, off in pending
        ]
        try:
            await asyncio.gather(*chunk_tasks)
        # conformance: ignore[E004] chunked-download error handler; cancels siblings, closes fd, checkpoints or cleans up, emits the terminal event/metric, then re-raises via StorageError chain
        except Exception as exc:
            # gather() does NOT cancel sibling tasks on first failure — without
            # this drain, orphaned chunk coroutines would keep running and write
            # into the fd after it is closed below (and, if the fd number were
            # reused, into an unrelated file). Cancel and await them all before
            # touching the fd. (BLDX-1523; latent since the original BLDX-1155
            # implementation.)
            for _t in chunk_tasks:
                _t.cancel()
            await asyncio.gather(*chunk_tasks, return_exceptions=True)
            os.close(fd)
            _log_storage_event(
                logging.WARNING,
                "download",
                key,
                outcome="failure",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                size_bytes=progress.fetched_bytes,
                error_class=_exc_class_name(exc),
            )
            if _handle_chunk_failure(
                exc, key=key, path=path, resume=resume, attempt=attempt
            ):
                # First 412: restart fresh once against the new generation.
                file_size = None
                etag = None
                continue
        # conformance: ignore[E004] cancellation pass-through; drains siblings, closes fd, re-raises immediately with no swallow
        except BaseException:
            # Task cancellation (e.g. Temporal activity cancel / worker
            # shutdown) is a BaseException, so the handler above never sees
            # it — but an orphaned chunk task writing into a reused fd does
            # not care WHY we exited. Same drain + close as the failure path;
            # the partial file + checkpoint stay on disk for resume. Re-raise
            # to preserve cancellation semantics.
            for _t in chunk_tasks:
                _t.cancel()
            await asyncio.gather(*chunk_tasks, return_exceptions=True)
            os.close(fd)
            raise
        else:
            os.close(fd)
            # Success: the checkpoint's existence means "incomplete" — remove
            # it so the file is observably complete.
            state_path.unlink(missing_ok=True)
            _log_storage_event(
                logging.DEBUG,
                "download",
                key,
                outcome="success",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                size_bytes=progress.fetched_bytes,
            )

            if not compute_hash:
                return None

            h = hashlib.sha256()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
