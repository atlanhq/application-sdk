"""Atomic artifact writes, and the typed failure when the disk is full (FND-318).

Why this module exists
----------------------
A write that fails part-way through leaves a truncated file **at the
artifact's real name**. Nothing downstream can tell that file from a correct
one: it is where it is supposed to be, so it is carried forward, uploaded, and
integrity-checked against its own truncated bytes (see
:mod:`application_sdk.storage.integrity` — the transfer layer faithfully
records exactly the bytes that moved, because exactly the intended bytes *did*
move). The failure surfaces much later, in a *consuming* app's parser, at a
byte offset that is identical on every retry.

The SDK already knew the fix and applied it only to its own bookkeeping — the
resume checkpoint in :mod:`application_sdk.storage.chunked` and the local
secrets file in :mod:`application_sdk.handler.service` are both written
temp → fsync → ``os.replace``. Every artifact an *app* produced was written
direct-to-final-name. This module inverts that back: the SDK owns the writers
apps actually use, so an app should have to go out of its way (a raw ``open()``)
to produce a partial artifact, not go out of its way to avoid one.

The guarantee
-------------
A partial artifact becomes **unnameable**. The final path either does not exist
or holds a complete file — there is no observable state in between, because the
bytes are written somewhere else entirely and the final path only ever comes
into existence via ``os.replace``, which is atomic on POSIX and on Windows.

Three parts make that hold:

* **The staging file is in a sibling directory, not a suffix.** A
  ``foo.json.tmp`` sitting next to ``foo.json`` is picked up by every walk of
  the artifact directory — a prefix upload would ship it, a directory
  ``FileReference`` would adopt it. Staging goes in
  :data:`~application_sdk.common._listing.PARTIAL_DIRNAME`, a directory both
  the SDK's tree walkers know to skip, and the *same parent* as the artifact so
  the publish is a same-filesystem rename rather than a copy.
* **fsync happens before the replace, inside the guard.** This is not about
  durability across node loss — the failure this targets is a step failure, and
  the page cache survives that (the argument
  ``storage.chunked._save_transfer_state`` already makes). It is about *when
  ENOSPC is reported*: on a delayed-allocation filesystem the ``write`` calls
  can all succeed and the error only materialise at flush. Without an fsync
  before the rename, a short write can be published as a complete artifact and
  no error is raised anywhere — precisely the incident.
* **The staging file is removed on every exit path.** Failure leaves nothing
  behind, and in particular nothing holding the blocks that the retry needs.

What it does not cover
----------------------
An app that opens its own file handle bypasses all of this, and so does any SDK
writer that *appends* to an artifact across calls — you cannot atomically
append without rewriting the whole file, and
:meth:`~application_sdk.storage.formats.json.JsonFileWriter._write_chunk` does
exactly that. Those paths still get :func:`disk_full_guard`, so an ``ENOSPC``
there is typed and attributed even though the file is not atomic.
"""

from __future__ import annotations

import errno
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, NoReturn

from application_sdk.common._listing import PARTIAL_DIRNAME
from application_sdk.common.path import convert_to_extended_path

__all__ = [
    "PARTIAL_DIRNAME",
    "atomic_copy",
    "atomic_path",
    "atomic_write",
    "disk_full_guard",
    "ensure_free_space",
]

#: ``errno`` values that mean "this filesystem has no room for your write".
#: ``EDQUOT`` is the same event seen from a quota rather than from free blocks,
#: and it needs the same response from whoever is on call, so both map to one
#: type rather than making every caller learn the distinction. Resolved via
#: ``getattr`` because ``EDQUOT`` is absent on some platforms.
_DISK_FULL_ERRNOS: frozenset[int] = frozenset(
    value
    for value in (getattr(errno, name, None) for name in ("ENOSPC", "EDQUOT"))
    if value is not None
)


def _format_bytes(count: int) -> str:
    """Render a byte count the way an operator sizing a volume would read it."""
    if count < 1024:
        return f"{count} B"
    size = float(count)
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.1f} {unit}"
    return f"{size / 1024.0:.1f} TiB"


def _nearest_existing_dir(path: Path) -> Path | None:
    """Return the closest ancestor of *path* that exists, for a free-space probe.

    ``shutil.disk_usage`` needs a path that exists; the directory a write is
    about to create does not yet. Walking up finds the mount point the write
    will land on, which is what the probe is actually asking about.
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def _free_bytes(path: Path) -> int | None:
    """Free bytes on the filesystem holding *path*, or ``None`` if unprobeable."""
    probe = _nearest_existing_dir(path)
    if probe is None:
        return None
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None  # conformance: ignore[E007] an unprobeable filesystem costs only the free-bytes number on an error already being raised; a log here would fire per failure with nothing actionable in it


def _is_disk_full(exc: OSError) -> bool:
    """Return ``True`` when *exc* is the filesystem saying it has no room."""
    return exc.errno in _DISK_FULL_ERRNOS


def _raise_disk_full(
    exc: OSError,
    *,
    path: Path,
    operation: str,
    required_bytes: int | None = None,
) -> NoReturn:
    """Re-raise a disk-full ``OSError`` as the typed error, chained to its cause."""
    from application_sdk.errors import DiskFullError  # noqa: PLC0415

    free = _free_bytes(path)
    detail = f"{operation} of '{path}' failed: no space left on device"
    if free is not None:
        detail += f" ({_format_bytes(free)} free)"
    if required_bytes is not None:
        detail += f"; the write needs about {_format_bytes(required_bytes)}"
    raise DiskFullError(
        message=detail,
        path=str(path),
        operation=operation,
        required_bytes=required_bytes,
        free_bytes=free,
        limit=None if free is None else f"{free} bytes free",
        observed=None if required_bytes is None else f"{required_bytes} bytes needed",
        cause=exc,
    ) from exc


@contextmanager
def disk_full_guard(
    path: str | Path,
    *,
    operation: str,
    required_bytes: int | None = None,
) -> Iterator[None]:
    """Map a disk-full ``OSError`` raised inside the block to ``DiskFullError``.

    Every other ``OSError`` propagates untouched — this classifies one specific
    failure, it does not become a general I/O error wrapper.

    Use it directly only on writes that :func:`atomic_write` cannot cover (an
    append, a third-party writer holding its own handle for the whole run). The
    atomic helpers already apply it.

    Args:
        path: Artifact being written. Names the failure and locates the
            free-space probe.
        operation: Short phrase naming the step, used verbatim in the message
            and carried as evidence — "carry-forward copy", "marker write".
        required_bytes: Size the write needs, when the caller knows it. Turns
            the message from "no room" into "needs N, has M".
    """
    try:
        yield
    except OSError as exc:
        if not _is_disk_full(exc):
            raise
        _raise_disk_full(
            exc,
            path=Path(path),
            operation=operation,
            required_bytes=required_bytes,
        )


def ensure_free_space(
    path: str | Path,
    required_bytes: int,
    *,
    operation: str,
) -> None:
    """Fail before a large write that the filesystem plainly cannot hold.

    Turns "silently corrupt, forty minutes in" into "needs ~N GiB, has M, in
    five seconds". The comparison is strict — free space is not padded with an
    invented safety margin, because the SDK has no basis for picking one. That
    makes this a check for the *plainly impossible* write, not the marginal
    one; a write that clears this bar and still runs out is caught by
    :func:`disk_full_guard` and reported identically.

    A filesystem that cannot be probed at all is not treated as a failure: the
    write proceeds and its own error remains the signal.

    Args:
        path: Where the write will land.
        required_bytes: Bytes the write needs. Non-positive is a no-op.
        operation: Short phrase naming the step, for the message and evidence.

    Raises:
        DiskFullError: If the filesystem has less free space than *required_bytes*.
    """
    if required_bytes <= 0:
        return
    free = _free_bytes(Path(path))
    if free is None or free >= required_bytes:
        return

    from application_sdk.errors import DiskFullError  # noqa: PLC0415

    raise DiskFullError(
        message=(
            f"{operation} of '{path}' needs about {_format_bytes(required_bytes)} "
            f"but only {_format_bytes(free)} is free on that filesystem. Failing "
            f"before the write rather than part-way through it."
        ),
        path=str(path),
        operation=operation,
        required_bytes=required_bytes,
        free_bytes=free,
        limit=f"{free} bytes free",
        observed=f"{required_bytes} bytes needed",
    )


def _fsync_path(path: Path) -> None:
    """Flush *path* to the filesystem so a short write cannot pass as complete.

    Opened ``O_RDWR`` rather than ``O_RDONLY``: fsync on a read-only descriptor
    is fine on POSIX but not portable to Windows, and the file is ours — it was
    created moments ago inside the staging directory.
    """
    fd = os.open(
        convert_to_extended_path(path),
        os.O_RDWR | getattr(os, "O_BINARY", 0),
    )
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(path: Path) -> None:
    """Remove a staging file, best-effort.

    Called on the failure path, where the write has already raised and that
    error is the one worth surfacing. A leftover staging file is inert — it is
    in a directory no listing descends into — so failing the run a second time
    over the cleanup would trade a real signal for a cosmetic one.
    """
    try:
        os.unlink(convert_to_extended_path(path))
    # conformance: ignore[E002] best-effort cleanup on a path that is already failing; a leftover staging file is inert and never published
    except OSError:
        pass


@contextmanager
def atomic_path(
    path: str | Path,
    *,
    operation: str,
    required_bytes: int | None = None,
) -> Iterator[Path]:
    """Yield a staging path to write, and publish it onto *path* on clean exit.

    For writers that insist on owning the file themselves —
    ``pyarrow.parquet.write_table``, ``shutil.copy2``, anything taking a
    filename rather than a handle. Write to the yielded path; on a clean exit
    it is fsync'd and renamed onto *path* in one atomic step. On any exception
    it is removed and *path* is left exactly as it was.

    Use :func:`atomic_write` instead when you are writing bytes yourself.

    Args:
        path: Final artifact path. Its parent directory is created.
        operation: Short phrase naming the step, for the failure message.
        required_bytes: Size the write needs, when known — checked up front by
            :func:`ensure_free_space`.

    Yields:
        The staging path to write to.

    Raises:
        DiskFullError: If the filesystem is out of space, before or during
            the write.
        FileNotFoundError: If the block exits cleanly without writing anything
            to the staging path.
    """
    final = Path(path)
    # A child of the artifact's own directory: same filesystem, so publishing
    # is a rename and not a copy. Created, never removed — an empty staging
    # directory is invisible to every listing anyway, and removing it would
    # race a concurrent writer that has already created its own staging file
    # in there.
    staging_dir = final.parent / PARTIAL_DIRNAME
    # Inside the guard: creating the staging directory is itself a write, and
    # on a full filesystem it fails with the same ENOSPC the write would have
    # — so it is classified identically rather than escaping as a raw OSError.
    with disk_full_guard(final, operation=operation, required_bytes=required_bytes):
        os.makedirs(convert_to_extended_path(staging_dir), exist_ok=True)
    # uuid4 rather than a counter or the pid: two writers racing on the same
    # artifact name — a cancelled attempt's orphaned thread and its retry —
    # must not resolve the same staging path, and neither may see the other.
    staging = staging_dir / f"{final.name}.{uuid.uuid4().hex}"

    if required_bytes is not None:
        ensure_free_space(staging_dir, required_bytes, operation=operation)

    published = False
    try:
        with disk_full_guard(final, operation=operation, required_bytes=required_bytes):
            yield staging
            if not os.path.exists(convert_to_extended_path(staging)):
                raise FileNotFoundError(
                    errno.ENOENT,
                    f"{operation} of '{final}' wrote nothing to its staging path",
                    str(staging),
                )
            _fsync_path(staging)
            os.replace(
                convert_to_extended_path(staging), convert_to_extended_path(final)
            )
            published = True
    finally:
        if not published:
            _discard(staging)


@contextmanager
def atomic_write(
    path: str | Path,
    *,
    operation: str,
    mode: str = "wb",
    encoding: str | None = None,
    required_bytes: int | None = None,
    **open_kwargs: Any,
) -> Iterator[IO[Any]]:
    """Yield an open handle whose contents land at *path* only if the block succeeds.

    The canonical SDK artifact write. Everything :func:`atomic_path` guarantees,
    with the handle opened and flushed for you.

    ``mode`` must be a ``"w"`` mode. Append modes are rejected: the staging
    file starts empty, so ``"ab"`` here would silently drop everything already
    in the artifact rather than appending to it. Exclusive-create (``"x"``)
    modes are rejected too: publication is ``os.replace``, which overwrites, so
    the exclusivity would not survive the rename and an existing artifact would
    be clobbered by a mode claiming to refuse exactly that. The contract is
    last-writer-wins. A genuine append cannot be made atomic without rewriting
    the whole file — wrap it in :func:`disk_full_guard` instead and accept that
    it is not.

    Args:
        path: Final artifact path. Its parent directory is created.
        operation: Short phrase naming the step, for the failure message.
        mode: Any writing mode ``open`` accepts (``"w"``, ``"wb"``).
        encoding: Text encoding, for text modes.
        required_bytes: Size the write needs, when known.
        **open_kwargs: Forwarded to ``open``.

    Yields:
        The open handle to write to.

    Raises:
        DiskFullError: If the filesystem is out of space, before or during
            the write.
        AtomicWriteModeError: If *mode* is an append mode, an exclusive-create
            mode, or not a writing mode.
    """
    from application_sdk.common.errors import AtomicWriteModeError  # noqa: PLC0415

    if "a" in mode or "+" in mode:
        raise AtomicWriteModeError(
            message=(
                f"atomic_write does not support mode {mode!r}: the staging file "
                f"starts empty, so appending to it would discard the existing "
                f"artifact rather than extend it. Use disk_full_guard around the "
                f"append instead."
            ),
            constraint="a creating mode: w, wb",
            value_summary=mode,
        )
    if "x" in mode:
        raise AtomicWriteModeError(
            message=(
                f"atomic_write does not support mode {mode!r}: publication is "
                f"os.replace, which overwrites, so exclusive-create semantics "
                f"cannot survive the rename — an 'x' mode here would clobber an "
                f"existing artifact while claiming not to. The contract is "
                f"last-writer-wins; use a creating 'w' mode."
            ),
            constraint="a creating mode: w, wb",
            value_summary=mode,
        )
    if "w" not in mode:
        raise AtomicWriteModeError(
            message=f"atomic_write needs a writing mode, got {mode!r}",
            constraint="a creating mode: w, wb",
            value_summary=mode,
        )

    with atomic_path(
        path, operation=operation, required_bytes=required_bytes
    ) as staging:
        with open(
            convert_to_extended_path(staging),
            mode=mode,
            encoding=encoding,
            **open_kwargs,
        ) as handle:
            yield handle
            # Inside the handle's context and inside atomic_path's guard, so a
            # buffer that only fails to reach the filesystem here — the usual
            # shape of ENOSPC on a buffered write — still fails the write
            # rather than closing quietly and publishing a short file.
            handle.flush()
            os.fsync(handle.fileno())


def atomic_copy(
    src: str | Path,
    dst: str | Path,
    *,
    operation: str = "file copy",
) -> None:
    """Copy *src* to *dst* so that *dst* never exists in a partial state.

    ``shutil.copy2`` writes straight to its destination filename, so a copy
    interrupted by a full disk leaves a truncated file at the artifact's real
    name — the write that produced the incident behind FND-318. This stages the
    copy and publishes it with a rename instead.

    Metadata is preserved, as ``copy2`` does.

    Args:
        src: File to copy.
        dst: Destination path (a filename, not a directory).
        operation: Short phrase naming the step, for the failure message.

    Raises:
        DiskFullError: If the filesystem is out of space, before or during
            the copy.
    """
    source = Path(src)
    # Probed rather than assumed: this is the one write whose size is known
    # before it starts, which is what lets the failure say "needs N, has M".
    try:
        required = source.stat().st_size
    # conformance: ignore[E009] an unstat-able source fails in copy2 below with its own error; skipping the preflight only forgoes the better message
    except OSError:
        required = 0

    with atomic_path(
        dst, operation=operation, required_bytes=required or None
    ) as staging:
        shutil.copy2(
            convert_to_extended_path(source), convert_to_extended_path(staging)
        )
