"""Race-safe directory listing for the storage layer (PART-1148).

Addresses two distinct failure classes observed when listing a
directory immediately after writes:

1. ``pathlib.Path.rglob`` silently suppresses ``OSError`` mid-walk
   (cpython#146646, cpython#68308). Transient permission errors,
   stale-FD errors, and filesystem errors that surface during a
   recursive walk are swallowed, returning what was found so far as
   if it were the complete listing. The behavior was intentional but
   undocumented until Python 3.13. Using ``os.scandir``-based
   recursion surfaces those errors so the caller can decide to retry
   or fail loudly instead of returning a silent undercount.

2. On macOS APFS under concurrent I/O, the directory btree can lag a
   write that has already returned to userspace. ``F_FULLFSYNC`` on
   the directory FD forces the kernel (and the drive) to commit
   pending metadata before the listing reads it. On Linux,
   ``os.fsync(fd)`` on the directory FD acts as a durability barrier
   (page cache → device) and a cross-filesystem visibility barrier
   for networked / FUSE filesystems; local Linux filesystems already
   give same-process read-after-write without fsync, but the barrier
   earns its keep on remote mounts and keeps Linux at parity with the
   Darwin branch. On Windows the barrier is a no-op — NTFS does not
   exhibit the same race in practice, and the platform has no
   analogue to a POSIX directory FD.

These compose into a single primitive (``safe_list_directory``) used
at every place the SDK lists a directory that may have been written
in the same process:

- ``application_sdk.storage.transfer.upload`` (directory branch)
- ``application_sdk.storage.reference.persist_file_reference`` (dir)
- ``application_sdk.contracts.types.FileReference.from_local`` (dir)
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

# Darwin-specific fcntl op for "flush both the kernel buffer and the
# drive's own write cache". Apple's fsync(2) man page documents this
# as the right primitive when strict ordering of writes — or strict
# visibility of recent writes to other readers — is required. The
# value is from <sys/fcntl.h>; the constant is not exported by
# Python's ``fcntl`` module.
_DARWIN_F_FULLFSYNC = 51


def _flush_directory_metadata(path: Path) -> None:
    """Commit pending directory-metadata changes before listing.

    Platform behavior:
      - macOS: ``fcntl(F_FULLFSYNC)`` on the directory FD. ``fsync``
        on Darwin only flushes kernel buffers, not the drive's write
        cache, so it is insufficient for the visibility guarantee we
        need here.
      - Linux: ``os.fsync(fd)`` on the directory FD. Durability +
        cross-filesystem visibility barrier (NFS, FUSE). Local Linux
        filesystems already give same-process read-after-write
        without fsync, but the barrier earns its keep on networked
        mounts and provides parity with the Darwin branch — future
        maintainers should not remove the call "because Linux doesn't
        need it" without considering the remote-FS case.
      - Windows: no-op. No POSIX directory FD; ``os.O_DIRECTORY`` is
        not defined; and NTFS does not exhibit the listing-after-write
        race that motivates this barrier.

    Best-effort: any ``OSError`` from the fsync (read-only mount,
    unsupported by the device, etc.) is swallowed. The barrier is a
    defense-in-depth measure; the ``os.scandir`` recursion that
    follows surfaces real listing errors on its own.
    """
    if sys.platform == "win32":
        return
    # ``O_DIRECTORY`` is POSIX-only. Defensive — sys.platform check
    # above should already cover this on every supported runtime.
    if not hasattr(os, "O_DIRECTORY"):
        return

    try:
        fd = os.open(path, os.O_DIRECTORY)
    except OSError:
        # If we cannot even open the dir FD, the subsequent scandir
        # call will raise a more informative OSError. Don't mask it.
        return

    try:
        if sys.platform == "darwin":
            # Lazy import — fcntl is POSIX-only and would break a
            # Windows import even if guarded at runtime.
            import fcntl  # noqa: PLC0415

            try:
                fcntl.fcntl(fd, _DARWIN_F_FULLFSYNC)
                return
            except OSError:  # conformance: ignore[E002] F_FULLFSYNC unsupported on this device/FS; fall through to portable fsync
                # Some Darwin device / FS combinations don't honor
                # F_FULLFSYNC (e.g. tmpfs in a sandbox). Fall through
                # to the portable barrier.
                pass
        try:
            os.fsync(fd)
        except OSError:  # conformance: ignore[E002] best-effort barrier; real listing errors surface via os.scandir below
            # Best-effort barrier — the listing primitive below is
            # the actual correctness layer.
            pass
    finally:
        os.close(fd)


def _scandir_recursive(path: Path) -> Iterator[Path]:
    """Yield every regular file under ``path``, iteratively.

    Uses ``os.scandir`` directly instead of ``pathlib.Path.rglob``
    because the latter silently suppresses ``OSError`` during
    traversal (cpython#146646), making partial-result silent failures
    indistinguishable from legitimately-empty directories.

    Descent is iterative — an explicit stack of directories left to
    visit — rather than a recursive generator. Two production
    benefits over the naive recursive form:

      1. **One open directory FD at a time.** A recursive
         ``yield from`` generator keeps every enclosing
         ``with os.scandir(...)`` context open until each inner
         generator exhausts, pinning N FDs at depth N. The iterative
         form ``pop → scandir → yield/push → close`` holds at most
         one ``os.scandir`` iterator at any moment.
      2. **No recursion-limit ceiling.** Depth is bounded only by
         available memory for the stack list, not by Python's
         default ~1000 frames (which some test harnesses lower).

    This matches the iterative approach used internally by CPython's
    ``pathlib.rglob`` and by widely-deployed tree walkers like
    ``black``, ``ruff``, and ``mypy``.

    Symlinks are not followed: ``os.DirEntry.is_dir/is_file`` are
    called with ``follow_symlinks=False``. This prevents infinite
    loops on cyclic structures, matches the behavior of
    ``find -type f`` without ``-L``, and — as a behavior change vs
    ``Path.rglob`` — excludes symlink-to-file entries from the
    listing (see ``safe_list_directory`` docstring).
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

    Closes two classes of directory-listing transient that have been
    observed in production:

    - ``pathlib.Path.rglob`` silently suppresses ``OSError`` mid-walk
      (cpython#146646), causing partial or empty listings to look
      indistinguishable from clean empty directories.
    - On macOS APFS under concurrent I/O, the directory btree can be
      stale immediately after a write. ``F_FULLFSYNC`` on the
      directory FD forces the kernel and drive to commit the
      metadata before we read.

    Args:
        path: A directory path. Must exist and be a directory.

    Returns:
        A list of ``Path`` objects, one per regular file under the
        tree. Order is filesystem-dependent (callers that need a
        stable order should sort).

    Raises:
        OSError: If ``path`` does not exist, is not a directory, or a
            transient filesystem error occurs during traversal.
            Unlike ``pathlib.Path.rglob``, errors are surfaced rather
            than swallowed.

    Behavior change vs ``pathlib.Path.rglob``:
        Symlinks are NOT followed. ``Path.rglob("*")`` combined with
        ``Path.is_file()`` defaults to ``follow_symlinks=True`` and
        would include symlink-to-file entries in the listing. This
        primitive uses ``os.DirEntry.is_file(follow_symlinks=False)``
        and excludes them. None of the SDK's three callers
        (``storage.transfer.upload``, ``storage.reference.persist``,
        ``contracts.types.FileReference.from_local``) intentionally
        use symlink-to-file in production paths; new callers that
        need symlink-following should add an opt-in parameter rather
        than changing the default.
    """
    _flush_directory_metadata(path)
    return list(_scandir_recursive(path))
