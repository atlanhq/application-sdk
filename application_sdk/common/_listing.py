"""Race-safe directory listing.

Wraps ``os.scandir`` with a metadata-flush barrier so directories
written immediately before the listing return their full contents:

- ``pathlib.Path.rglob`` silently swallows ``OSError`` mid-walk
  (cpython#146646), conflating partial errors with empty trees.
  ``os.scandir`` surfaces the error instead.
- On macOS APFS under concurrent I/O the directory btree can lag a
  finished write. ``F_FULLFSYNC`` on the directory FD forces the
  commit before the listing reads it. On Linux ``os.fsync`` covers
  the equivalent NFS / FUSE case. On Windows the barrier is a no-op.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

# Darwin-only fcntl op: flush kernel buffer + drive cache. Value from
# <sys/fcntl.h>; not exported by Python's fcntl module.
_DARWIN_F_FULLFSYNC = 51


def _flush_directory_metadata(path: Path) -> None:
    """Best-effort metadata-flush barrier before listing.

    macOS: ``fcntl(F_FULLFSYNC)`` — Darwin's ``fsync`` does not flush
    the drive cache. Linux: ``os.fsync(fd)`` on the directory FD;
    covers NFS / FUSE where same-process read-after-write isn't
    automatic. Windows: no-op.

    Any ``OSError`` is swallowed; ``os.scandir`` below is the
    correctness layer.
    """
    if sys.platform == "win32" or not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_DIRECTORY)
    except OSError:
        return
    try:
        if sys.platform == "darwin":
            import fcntl  # noqa: PLC0415

            try:
                fcntl.fcntl(fd, _DARWIN_F_FULLFSYNC)
                return
            except OSError:  # conformance: ignore[E002] fall through to portable fsync on devices that don't honor F_FULLFSYNC
                pass
        try:
            os.fsync(fd)
        except OSError:  # conformance: ignore[E002] best-effort barrier; real errors surface via os.scandir
            pass
    finally:
        os.close(fd)


def _scandir_recursive(path: Path) -> Iterator[Path]:
    """Yield every regular file under ``path``, iteratively.

    Iterative descent (explicit stack) rather than recursive
    ``yield from``: bounds open directory FDs to one at any moment,
    has no recursion-limit ceiling, and matches the approach used
    internally by ``pathlib.rglob``, ``black``, and ``ruff``.

    Symlinks are not followed: prevents loops on cyclic structures
    and excludes symlink-to-file entries (a behaviour change vs
    ``Path.rglob`` — see ``safe_list_directory``).
    """
    stack: list[Path] = [path]
    while stack:
        current = stack.pop()
        with os.scandir(current) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)


def safe_list_directory(path: Path) -> list[Path]:
    """Recursively list every regular file under ``path``, race-safely.

    Wraps a metadata-flush barrier with an ``os.scandir`` descent.
    Surfaces ``OSError`` rather than swallowing it like
    ``pathlib.rglob`` does (cpython#146646), and forces APFS directory
    metadata to commit before reading on macOS.

    Args:
        path: A directory path. Must exist and be a directory.

    Returns:
        A list of ``Path`` objects, one per regular file under the
        tree. Order is filesystem-dependent.

    Raises:
        OSError: On missing path, non-directory, or any traversal
            error. Unlike ``pathlib.Path.rglob``, errors are surfaced.

    Behaviour change vs ``Path.rglob``: symlink-to-file entries are
    NOT included (``follow_symlinks=False`` on every check). New
    callers needing symlink-following should add an opt-in parameter
    rather than changing the default.
    """
    _flush_directory_metadata(path)
    return list(_scandir_recursive(path))
