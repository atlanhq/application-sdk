"""Race-safe directory listing, and what a listing is allowed to yield.

Wraps ``os.scandir`` with a metadata-flush barrier so directories
written immediately before the listing return their full contents:

- ``pathlib.Path.rglob`` silently swallows ``OSError`` mid-walk
  (cpython#146646), conflating partial errors with empty trees.
  ``os.scandir`` surfaces the error instead.
- On macOS APFS under concurrent I/O the directory btree can lag a
  finished write. ``F_FULLFSYNC`` on the directory FD forces the
  commit before the listing reads it. On Linux ``os.fsync`` covers
  the equivalent NFS / FUSE case. On Windows the barrier is a no-op.

This module also owns :data:`INTERNAL_DIRNAMES` — the directory names
the SDK uses for its own working files, which must never surface as
artifacts. It lives here rather than beside the writer that creates
them because *excluding* them is a property of every tree walk, and
there is more than one walker: ``safe_list_directory`` below and
``storage.batch.upload_prefix``. Both consult this one definition, so
"invisible to a listing" and "invisible to a prefix upload" cannot
drift apart. Keeping the vocabulary in this stdlib-only module also
means neither walker takes on a new import to honour it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

# Darwin-only fcntl op: flush kernel buffer + drive cache. Value from
# <sys/fcntl.h>; not exported by Python's fcntl module.
_DARWIN_F_FULLFSYNC = 51

#: Directory an atomic write stages its partial file in, sited as a
#: sibling of the artifact rather than as a suffix beside it — a
#: ``foo.json.tmp`` next to ``foo.json`` is picked up by every walk of
#: the artifact directory, which is the leak this name exists to avoid.
#: Written by :mod:`application_sdk.common.atomic` (FND-318).
PARTIAL_DIRNAME = ".sdk-partial"

#: Directory a ``Writer`` stages its whole output in until ``close()``
#: publishes it, sited as a sibling of the output directory (FND-317).
#: A sibling is out of reach of a walk of the *output* directory, but
#: not of a walk of the run root a level up — which is what a prefix
#: upload of a whole run does — so it belongs in the set below.
#: Re-exported as ``_STAGING_ROOT_DIRNAME`` by ``storage.formats``,
#: which owns the staging behaviour; defined here so the exclusion
#: stays a single fact and this module keeps its stdlib-only imports.
WRITER_STAGING_DIRNAME = ".sdk-writer-staging"

#: Every directory name that holds SDK working files rather than
#: artifacts. An explicit set rather than a ``.sdk-`` prefix rule: a
#: denylist that silently swallowed a customer directory because its
#: name matched a convention would be a data-loss bug, and the set is
#: short enough that adding to it is a one-line change. Add here when
#: introducing a new SDK-internal working directory — every walker
#: picks it up with no further edit.
INTERNAL_DIRNAMES: frozenset[str] = frozenset({PARTIAL_DIRNAME, WRITER_STAGING_DIRNAME})


def is_internal_dirname(name: str) -> bool:
    """Return ``True`` when *name* is an SDK working directory, not an artifact."""
    return name in INTERNAL_DIRNAMES


def prune_internal_dirs(dirnames: list[str]) -> None:
    """Drop SDK working directories from ``os.walk``'s ``dirnames``, in place.

    ``os.walk`` re-reads ``dirnames`` after yielding to decide where to
    descend, so mutating it in place — rather than filtering the copy —
    is what actually stops the descent.
    """
    dirnames[:] = [name for name in dirnames if not is_internal_dirname(name)]


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

    :data:`INTERNAL_DIRNAMES` are not descended into. The test is on
    the entry name during descent, never on ``path`` itself, so a
    caller that deliberately points the walk *at* one of these
    directories still gets its contents.
    """
    stack: list[Path] = [path]
    while stack:
        current = stack.pop()
        with os.scandir(current) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    if is_internal_dirname(entry.name):
                        continue
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

    Directories named in :data:`INTERNAL_DIRNAMES` are skipped — an
    in-flight atomic write must not surface as an artifact in a
    ``FileReference`` directory or anywhere else this listing feeds.
    """
    _flush_directory_metadata(path)
    return list(_scandir_recursive(path))


def file_sizes(paths: list[Path]) -> list[int]:
    """Return ``st_size`` for every path, in order.

    One blocking pass over a listing so a transfer can size its fan-out per
    file without a ``stat`` on the event loop for each of thousands of files.
    Run it via ``run_in_thread``, like :func:`safe_list_directory`.

    Raises:
        OSError: If any path cannot be stat-ed (removed between listing and
            sizing, permission denied). Surfaced, not swallowed, for the same
            reason :func:`safe_list_directory` surfaces traversal errors.
    """
    return [p.stat().st_size for p in paths]
