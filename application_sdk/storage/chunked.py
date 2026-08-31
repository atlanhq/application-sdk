"""Chunked, resumable, version-pinned downloads (BLDX-1513 / BLDX-1523).

Split out of ``storage/ops.py``: this module owns the parallel range-GET
download path and its resume-checkpoint sidecar, leaving ``ops`` to
single-request I/O primitives. ``download_file_chunked`` remains importable
from both ``application_sdk.storage`` and ``application_sdk.storage.ops``
(back-compat re-export).

Resumable chunked-download state
--------------------------------
A chunked download writes ranges at fixed offsets into a pre-allocated file.
Both in-flight files live in ``.sdk-partial/`` beside the destination —
``{name}.part`` for the data and ``{name}.transfer-state`` for the checkpoint
— so neither is ever adopted by a directory ``FileReference`` or shipped by a
prefix upload.
The sidecar records which chunk indices have landed on disk, plus the identity
of the remote object they came from (key / size / chunk size / etag). Its
*existence* is the "incomplete" marker: it is deleted only after the
``os.replace`` publish, so an unpublished part file always has its checkpoint
and a failed publish resumes without re-fetching a single chunk. A stale
checkpoint that outlives its part file (publish succeeded, unlink did not) is
discarded by validation, which requires the part to exist at the preallocated
size. On retry after a crash the download resumes by fetching only the missing
chunk indices — valid only while the remote object is unchanged, which is what
the etag match enforces.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import obstore
import orjson

from application_sdk._runtime.offload import run_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.common._listing import PARTIAL_DIRNAME
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage._locks import PathLockRegistry
from application_sdk.storage._telemetry import _log_transfer_progress

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.storage.ops import BoundStore

logger = get_logger(__name__)

#: Per-destination writer exclusion for the chunked transfer primitive.
#:
#: ``_part_path`` and ``_transfer_state_path`` are deterministic functions of
#: the destination, so every caller of the public ``download_file_chunked``
#: shares one staging file and one checkpoint per ``local_path`` — not just
#: callers routed through ``materialize_file_reference``. The lock lives here,
#: on the resource, so two concurrent downloads to one destination cannot
#: interleave offset writes into one ``.part`` or corrupt each other's
#: checkpoint (CONNECT-1126 review). A separate registry from the materialise
#: guard's, so a materialise caller already holding its per-path lock nests
#: into this one without self-deadlock.
_TRANSFER_LOCKS = PathLockRegistry("storage.download.lock_wait")

_TRANSFER_STATE_SUFFIX = ".transfer-state"


def _transfer_state_path(path: Path) -> Path:
    """Sidecar path holding resumable-download state for *path*.

    Staged inside :data:`~application_sdk.common._listing.PARTIAL_DIRNAME`
    beside the part file, for the same reason the part file lives there: the
    tree-walk exclusions are directory-level only, so a checkpoint named
    ``foo.bin.transfer-state`` beside the destination would be adopted by a
    directory ``FileReference`` or shipped by a prefix upload after a failed
    resume-enabled download. A checkpoint written by a pre-upgrade pod at the
    old beside-the-destination location is ignored, which costs one
    re-download of an already-failed transfer.
    """
    return path.parent / PARTIAL_DIRNAME / (path.name + _TRANSFER_STATE_SUFFIX)


def _part_path(path: Path) -> Path:
    """In-flight data file for *path* — published to *path* via ``os.replace``.

    Deterministic (not a random temp name) so a Temporal retry on the same pod
    can resume it from the checkpoint sidecar. The destination path itself only
    ever holds a complete file, so concurrent readers of a shared ``local_path``
    never see preallocated zeros or half-written chunks (CONNECT-1126).

    Staged inside :data:`~application_sdk.common._listing.PARTIAL_DIRNAME` in
    the destination's own directory: same filesystem (so publish is a rename)
    and invisible to every SDK tree walk, so a stranded part file can never be
    adopted by a directory ``FileReference`` or shipped by a prefix upload.
    """
    return path.parent / PARTIAL_DIRNAME / (path.name + ".part")


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
    """Remove the in-flight part file and checkpoint sidecar (best-effort).

    The destination itself is left alone: it only ever holds a previously
    published complete generation, which stays valid — and stays useful to
    its readers — even when the stored object was rewritten (412) or deleted
    (404) mid-download (CONNECT-1126).

    "Best-effort" is enforced here rather than promised: ``missing_ok`` covers
    only ``FileNotFoundError``, so without the suppression an undeletable
    staging file (EPERM, EBUSY, a read-only remount) would propagate out of
    every caller and *replace* the result they were reporting — the 412/404
    path in :func:`_handle_chunk_failure` would raise ``OSError`` in place of
    the typed ``StorageNotFoundError`` / ``StorageError``, and the owned-temp
    cleanups in ``storage.transfer`` would fail a copy that had already
    succeeded. Leaving a file behind is the lesser fault in every one of
    those cases, and the second unlink is attempted even when the first
    fails.
    """
    for staging in (_part_path(path), _transfer_state_path(path)):
        with contextlib.suppress(OSError):
            staging.unlink(missing_ok=True)


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
    returns an empty set (fresh download). The delete is best-effort: an
    undeletable bad sidecar must not fail the download either — the fresh
    download that follows overwrites or re-discards it.
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
    with contextlib.suppress(OSError):
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
    """Fetch one range and write it at its fixed offset; checkpoint + heartbeat.

    Raises:
        StorageError: If the store served fewer bytes than the requested range.
            The file is pre-allocated to its full size, so a short range would
            otherwise leave a hole of NUL bytes that reads back as a
            plausible-looking file of exactly the right length.
    """
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
        if len(raw) != length:
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                StorageError,
            )

            raise StorageError(
                f"Short range read on chunked download of '{key}': requested "
                f"{length} bytes at offset {offset}, received {len(raw)}.",
                key=key,
            )
        # lseek+write instead of pwrite (Windows lacks pwrite). Safe only
        # because asyncio is single-threaded: no await between the two
        # calls means no other coroutine can interleave on the fd position.
        # WARNING: if _fetch_chunk is ever moved into a worker thread (e.g. via
        # run_in_thread), lseek+write becomes a data race — two threads
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
        # One range GET landed at its offset — the unit of work for this path
        # (ADR-0018).
        current_progress_tracker().mark_progress("storage.download_range")
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
        # Legacy behaviour: no checkpoint, so an in-flight part file is
        # garbage. The destination itself is untouched — it only ever holds
        # a previous complete generation. Suppressed for the same reason
        # _discard_transfer_state suppresses: this runs immediately before the
        # typed raise below, so an undeletable part file would replace the
        # transfer's real error with a bare OSError.
        with contextlib.suppress(OSError):
            _part_path(path).unlink(missing_ok=True)
    # With resume enabled the part file + sidecar stay on disk —
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
    verify: bool | None = None,
    expected_sha256: str | None = None,
    sidecar_present: bool | None = None,
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

    **Resume requires a pinned generation (FND-306).** Resume is honoured only
    when an etag is known. Without one the range GETs are unpinned, so reusing
    a previous attempt's chunks could splice two generations of a rewritten
    object into a file of exactly the right length — and on a download with no
    sidecar, nothing downstream could tell. An unpinned download therefore
    always starts fresh.

    **Resume (BLDX-1523):** when *resume* is enabled, completed chunk indices
    are checkpointed to a ``.sdk-partial/{name}.transfer-state`` sidecar
    beside the part file after every chunk write, and an interrupted download
    leaves the in-flight part file + sidecar on disk instead of deleting
    them. A retry that resolves the
    same object generation (key / size / chunk size / etag all match)
    re-fetches only the missing chunks. The sidecar is deleted on success, so
    a data file without one is always complete. Resume requires the retry to
    see the same local filesystem (same pod or persistent volume); after node
    loss the download simply starts fresh.

    **Atomic publish (CONNECT-1126):** chunks are written to a deterministic
    part file inside the ``.sdk-partial`` staging directory beside the
    destination (see :func:`_part_path`), which lands at *local_path* via
    ``os.replace`` on success. The destination never holds preallocated zeros
    or a partial file, so concurrent readers of a shared ``local_path`` see
    only a complete previous or new generation. On Windows the publish
    additionally requires that no other handle holds the destination open:
    ``os.replace`` raises ``PermissionError`` rather than succeeding, so a
    concurrent reader turns a corrupt read into a failed write.

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
        verify: Validate the completed file against the digest its producer
            recorded (FND-306).  ``None`` (default) follows
            ``ATLAN_STORAGE_VERIFY_TRANSFERS``.  Unlike the single-stream path,
            verifying here costs a re-read of the finished file (chunks land
            out of order, so they cannot feed one hasher) — the re-read runs in
            a worker thread so it never blocks the heartbeat loop.
        expected_sha256: Digest to verify against.  Pass it when the caller
            already holds the producer's digest — the sidecar lookup is then
            skipped, saving a round trip.  ``None`` (default) fetches the
            sidecar when verification is on.
        sidecar_present: Whether ``{key}.sha256`` exists, when a prior listing
            already established it.  Saves the existence probe on prefix
            downloads, where the listing has already enumerated every sidecar.

    Returns:
        Hex-encoded SHA-256 digest if *compute_hash* is ``True``, else ``None``.

    Raises:
        StorageNotFoundError: If *key* does not exist.
        StorageError: If a chunk download or the disk write fails, a range came
            back short, or the object was rewritten during both the original
            and restarted attempt.
        StorageIntegrityError: If the completed file does not match the digest
            its producer recorded.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: ops re-exports download_file_chunked for back-compat
        _resolve_store,
        normalize_key,
    )

    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    async with _TRANSFER_LOCKS.guard(str(path)):
        return await _download_chunked_locked(
            resolved,
            key,
            path,
            chunk_size_bytes=chunk_size_bytes,
            max_concurrent_chunks=max_concurrent_chunks,
            compute_hash=compute_hash,
            file_size=file_size,
            etag=etag,
            resume=resume,
            verify=verify,
            expected_sha256=expected_sha256,
            sidecar_present=sidecar_present,
        )


async def _download_chunked_locked(
    resolved: "ObjectStore",
    key: str,
    path: Path,
    *,
    chunk_size_bytes: int,
    max_concurrent_chunks: int,
    compute_hash: bool,
    file_size: int | None,
    etag: str | None,
    resume: bool | None,
    verify: bool | None,
    expected_sha256: str | None,
    sidecar_present: bool | None,
) -> str | None:
    """Body of :func:`download_file_chunked`, run under the destination lock.

    The caller holds ``_TRANSFER_LOCKS`` for *path* across everything here —
    checkpoint load, part-file preallocation, chunk writes, and the
    ``os.replace`` publish — so the deterministic staging files are only ever
    touched by one writer at a time.
    """
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: ops re-exports download_file_chunked for back-compat
        _exc_class_name,
        _is_not_found,
        _log_storage_event,
        download_file,
    )

    if resume is None:
        from application_sdk.constants import STORAGE_RESUME_DOWNLOADS  # noqa: PLC0415

        resume = STORAGE_RESUME_DOWNLOADS

    from application_sdk.storage import (  # noqa: PLC0415 — circular: ops imports this module at top level
        integrity,
    )

    verifying = integrity.verification_enabled(verify)

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
        part = _part_path(path)

        # Small files: delegate to the single-stream path so they still use the
        # streaming GET (avoids materialising the whole body via range GETs).
        # Drop any stale checkpoint and part file from an earlier, larger
        # object generation — the delegate never opens the part file, so
        # nothing else would ever remove it. Through the helper, so an
        # undeletable leftover cannot fail this download before it starts:
        # stale state we could not clear is strictly better than no download,
        # and the delegate publishes to the destination without consulting it.
        if size <= chunk_size_bytes:
            _discard_transfer_state(path)
            return await download_file(
                key,
                path,
                resolved,
                compute_hash=compute_hash,
                normalize=False,
                # Resolved above — pass through so the delegate does not
                # re-decide the kill-switch and an explicit verify=False holds.
                verify=verifying,
                expected_sha256=expected_sha256,
                sidecar_present=sidecar_present,
            )

        # Resume needs something that pins the object generation. The etag is
        # that something: every range GET carries If-Match, so chunks cannot
        # come from two generations. Without one — a store whose HEAD returns
        # no e_tag, or a caller that supplied file_size without an etag — the
        # range GETs are unpinned, and reusing a previous attempt's chunks
        # would silently splice the old generation onto the new one if the
        # object were rewritten in between. The result is a file of exactly
        # the right length that reads as a clean transfer.
        #
        # The digest check added in FND-306 catches that, but only where a
        # sidecar exists; on a bare download of an object nobody wrote a
        # sidecar for, nothing would. So an unpinned download always starts
        # fresh: correctness over the bandwidth a resume would have saved, in
        # the one case where a mistake is undetectable.
        can_resume = resume and etag is not None
        if resume and etag is None:
            # Both staging files go, not just the checkpoint: without an etag
            # this download always starts fresh, so the part file is about to
            # be truncated anyway and keeping it buys nothing. Through the
            # helper so an undeletable leftover cannot fail the download before
            # it starts, which would surface as a bare OSError where the caller
            # expects a fresh download or the transfer's own typed error.
            _discard_transfer_state(path)
        done: set[int] = (
            _load_and_validate_checkpoint(
                state_path,
                key=key,
                size=size,
                chunk_size_bytes=chunk_size_bytes,
                etag=etag,
                path=part,
            )
            if can_resume
            else set()
        )
        resuming = bool(done)

        # Pre-allocate the in-flight ``.part`` file at the target size so lseek
        # can address any offset; the destination path is only written by the
        # os.replace publish on success (CONNECT-1126). On resume, open WITHOUT
        # O_TRUNC so completed chunks survive.
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
        os.makedirs(part.parent, mode=0o700, exist_ok=True)
        fd = os.open(str(part), flags, 0o600)
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
                    # can_resume, not resume: checkpointing an unpinned
                    # download only strands a sidecar no attempt may use.
                    resume=can_resume,
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
                # can_resume: with nothing pinning the generation the partial
                # file can never be reused, so it is garbage — delete it rather
                # than leave it for an attempt that will start over anyway.
                exc,
                key=key,
                path=path,
                resume=can_resume,
                attempt=attempt,
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
            # Success: fsync so a delayed-allocation ENOSPC surfaces here
            # rather than publishing a short file (offloaded — a multi-GB
            # flush must not hold the event loop and the heartbeat with it),
            # then atomically publish the complete file at the destination.
            # Publish-first, so any failure here — fsync or rename — leaves
            # part + checkpoint intact as a valid resume state: the retry
            # finds every chunk done and only re-runs this publish, instead
            # of re-fetching a multi-GB object. The checkpoint is dropped
            # below, OUTSIDE this guard: once the publish has succeeded a
            # failed unlink must not report the download as failed — a
            # checkpoint that outlives its part file is discarded by
            # validation on the next attempt, so removal is best-effort.
            # conformance: ignore[E004] publish error handler; _log_storage_event records error_class and exception is re-raised via StorageError chain
            try:
                try:
                    await run_in_thread(os.fsync, fd)
                finally:
                    os.close(fd)
                os.replace(part, path)
            except Exception as exc:
                _log_storage_event(
                    logging.WARNING,
                    "download",
                    key,
                    outcome="failure",
                    elapsed_ms=(time.monotonic() - started) * 1000.0,
                    size_bytes=progress.fetched_bytes,
                    error_class=_exc_class_name(exc),
                )
                from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                    StorageError,
                )

                raise StorageError(
                    f"Failed to publish downloaded file to '{path}'",
                    key=key,
                    cause=exc,
                ) from exc
            with contextlib.suppress(OSError):
                state_path.unlink(missing_ok=True)
            _log_storage_event(
                logging.DEBUG,
                "download",
                key,
                outcome="success",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                size_bytes=progress.fetched_bytes,
            )

            # Every range was requested and every one returned its full
            # length (enforced per chunk in _fetch_chunk), so the file is
            # complete at the pre-allocated size. What that cannot tell us is
            # whether the *object* was intact to begin with — that needs the
            # producer's digest (FND-306). Look it up only now: a store-wide
            # failure has already surfaced as a chunk error above, so anything
            # that goes wrong here is genuinely sidecar-specific.
            if verifying and expected_sha256 is None:
                expected_sha256 = await integrity.read_expected_digest(
                    resolved, key, sidecar_present=sidecar_present
                )
            if not compute_hash and expected_sha256 is None:
                return None

            # Chunks land out of order, so no single hasher can be fed during
            # the transfer: both the caller's digest and the integrity check
            # cost one re-read of the finished file. ``integrity.sha256_file``
            # runs it through ``run_in_thread`` — inline, a multi-GB re-read
            # holds the event loop, and with it the enclosing activity's
            # auto-heartbeat, for the full read+hash, so a transfer that
            # actually succeeded gets retried (ADR-0010, P031; FND-282 made
            # this offload here, FND-306 moved the body to the shared helper).
            digest = await integrity.sha256_file(path)
            if verifying and expected_sha256 is not None:
                integrity.check_transfer_digest(
                    "download",
                    key,
                    expected=expected_sha256,
                    actual=digest,
                    local_path=path,
                )
            return digest if compute_hash else None
