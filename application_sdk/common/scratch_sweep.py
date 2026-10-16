"""Delete what a dead worker left under ``TEMPORARY_PATH``, once, before the
worker polls.

Why this exists
---------------
A worker pod mounts its own volume for scratch, and the volume lives exactly as
long as the pod. A container killed for memory is restarted by the kubelet in
the same pod, on the same volume, so everything the dead process had written -
random-named temp directories, query-engine spill files, partial downloads - is
still there when its replacement starts. Nothing ever removes it, so each crash
adds another process's worth, and a pod caught in a crash loop grows its disk
without bound while its healthy siblings stay small. A production RCA found a
crash-looping worker holding more than ten times the scratch of any healthy pod
in the same deployment.

When a worker process starts, no other process that owns files under
``TEMPORARY_PATH`` is running in its container, so what is already there was
left by one that is gone. The exceptions are kept; see *What is never deleted*.

When it runs
------------
Once per process, from ``run_worker_mode`` in ``application_sdk.main``, before
the infrastructure, the app module or the Temporal worker is created, so
nothing the new process makes under ``TEMPORARY_PATH`` can predate the sweep.
Not from the restart supervisor: an in-process worker rebuild can leave orphaned
activity threads still writing. Not in handler mode, which runs no activities,
and not in combined mode, which serves SDR, docker-compose and local
development, where the volume layout is the deployer's and need not be the
pod's own.

By default it runs only in Kubernetes; ``ATLAN_TEMPORARY_PATH_SWEEP_ON_START``
switches it either way (see
``application_sdk.constants.TEMPORARY_PATH_SWEEP_ON_START``).

What it deletes
---------------
The children of ``TEMPORARY_PATH`` - files, directories, links - and never
``TEMPORARY_PATH`` itself, which may be a mount point and is where the new
process writes next. A symbolic link is unlinked, never followed, so nothing
outside ``TEMPORARY_PATH`` is reachable through one. The sweep never descends
into a directory on another filesystem either: a volume mounted inside
``TEMPORARY_PATH`` is not this container's scratch to clear.

What is never deleted
---------------------
:data:`BUILTIN_KEEP`, the process's temporary-file directory, and whatever
``ATLAN_TEMPORARY_PATH_SWEEP_KEEP`` names:

* The SDK's telemetry staging directory,
  ``artifacts/apps/<app>/<deployment>/observability``. Log, metric, trace and
  sizing batches are written there and uploaded by the process that wrote
  them, so nothing in it is read back on a later start, but it is live while
  the sweep runs: this process's own sinks write there from import onwards, and
  in SDR the daprd log forwarder, a sibling process the entrypoint starts first,
  writes there too. Sweeping it would race their uploads. A dead process leaves
  only its last unflushed batch there, kilobytes.
* ``lost+found``, the filesystem's own directory when a volume is mounted at
  ``TEMPORARY_PATH`` itself: never ours, and not removable by a non-root worker.
* The directory :mod:`tempfile` writes to, when ``TMPDIR`` points inside
  ``TEMPORARY_PATH``. Python resolves it once and never recreates it, so
  removing it would fail every later temporary file.

Nothing else the SDK writes there has to survive a restart. Run outputs,
staging files, downloaded credential files and partial transfers are scratch
for one attempt. The rest are caches whose loss costs a download, never a wrong
answer: a ``FileReference`` copy is reused only once its SHA-256 matches, the
current-state mirror re-checks every file against its ``.sdk-sync`` index, and
a resumable download's ``.sdk-partial`` checkpoint lets a retry skip finished
chunks only when that retry lands on this pod. One that lands elsewhere never
reads it, which is the leak this module exists to stop.

When it refuses
---------------
It deletes nothing, and logs a warning, when ``TEMPORARY_PATH`` resolves to a
path with fewer than two components (``/``, ``/tmp``), to the home directory or
a directory containing it, or to the working directory the app runs from or a
directory containing it, and when ``ATLAN_TEMPORARY_PATH_SWEEP_KEEP`` holds a
pattern it cannot honour (absolute, or climbing out with ``..``). A missing
``TEMPORARY_PATH`` is nothing to sweep. No failure stops the worker: what it
cannot remove is counted and reported in one warning, and anything unexpected
is logged once and swallowed.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from application_sdk import constants
from application_sdk.constants import OBSERVABILITY_DIR
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: Kept by every sweep, on top of ``ATLAN_TEMPORARY_PATH_SWEEP_KEEP``. Same
#: pattern rules as that variable; the module docstring says why each is here.
BUILTIN_KEEP: tuple[str, ...] = (
    OBSERVABILITY_DIR.format(application_name="*", deployment_name="*"),
    "lost+found",
)

_GIB = 1024**3

#: A keep rule: one glob per path component, matched against an entry's path
#: relative to the swept root.
_Rule = tuple[str, ...]


class SweepStatus(str, Enum):
    """How a sweep ended."""

    #: Switched off; nothing was looked at.
    OFF = "off"
    #: An unsafe root or keep pattern; nothing was deleted.
    REFUSED = "refused"
    #: The root does not exist, or is not a directory.
    NOTHING_TO_SWEEP = "nothing_to_sweep"
    #: The root was walked; the counts say what happened, failures included.
    SWEPT = "swept"
    #: Something unexpected stopped the walk part-way.
    FAILED = "failed"


@dataclass(frozen=True)
class SweepResult:
    """What one sweep did.

    Attributes:
        status: How the sweep ended.
        removed_entries: Files, links and directories deleted.
        removed_bytes: Disk space the deleted files held: their allocated
            blocks where the platform reports them, their size elsewhere.
        kept_entries: Entries left in place, because a keep pattern matched or
            because they are another filesystem's mount point.
        failed_entries: Entries the sweep tried and failed to delete.
    """

    status: SweepStatus
    removed_entries: int = 0
    removed_bytes: int = 0
    kept_entries: int = 0
    failed_entries: int = 0


def sweep_temporary_path(
    root: str | os.PathLike[str] | None = None,
    *,
    enabled: bool | None = None,
    keep: Iterable[str] | None = None,
) -> SweepResult:
    """Delete what an earlier process left under ``TEMPORARY_PATH``.

    Call once per process, at worker start, before anything in the process
    writes scratch. Never raises: a worker that cannot clean up still starts.
    Blocking file I/O, so call it from a thread when an event loop is running.

    Args:
        root: The directory to sweep. Defaults to ``constants.TEMPORARY_PATH``,
            read at call time.
        enabled: Whether to sweep at all. Defaults to
            ``constants.TEMPORARY_PATH_SWEEP_ON_START``.
        keep: Glob patterns, relative to *root*, naming entries to keep.
            Defaults to ``constants.TEMPORARY_PATH_SWEEP_KEEP``.
            :data:`BUILTIN_KEEP` applies on top either way.

    Returns:
        What the sweep did. It is also logged: one INFO line, plus one WARNING
        when the sweep refused, failed, or could not remove something.

    Example:
        >>> from application_sdk.common.scratch_sweep import sweep_temporary_path
        >>> sweep_temporary_path(enabled=False).status
        <SweepStatus.OFF: 'off'>
    """
    if enabled is None:
        enabled = constants.TEMPORARY_PATH_SWEEP_ON_START
    target = constants.TEMPORARY_PATH if root is None else root
    if not enabled:
        logger.info(
            "Scratch sweep at worker start: off "
            "(ATLAN_TEMPORARY_PATH_SWEEP_ON_START=%s kubernetes=%s)",
            os.getenv("ATLAN_TEMPORARY_PATH_SWEEP_ON_START", "").strip() or "unset",
            "yes" if os.getenv("KUBERNETES_SERVICE_HOST", "").strip() else "no",
        )
        return SweepResult(SweepStatus.OFF)

    patterns = constants.TEMPORARY_PATH_SWEEP_KEEP if keep is None else tuple(keep)
    try:
        return _sweep(Path(target), patterns)
    except Exception as exc:
        # No cleanup is worth a worker that does not start: the worst outcome
        # allowed here is a disk left as it was found.
        logger.warning(
            "Scratch sweep at worker start failed with %s under %s; "
            "the worker starts anyway",
            type(exc).__name__,
            target,
            exc_info=True,
        )
        return SweepResult(SweepStatus.FAILED)


def _sweep(target: Path, patterns: tuple[str, ...]) -> SweepResult:
    """Check the root and the keep patterns, then walk and delete."""
    rules, unusable = _parse_keep(patterns)
    if unusable:
        logger.warning(
            "Scratch sweep at worker start: refusing to sweep %s, because "
            "ATLAN_TEMPORARY_PATH_SWEEP_KEEP has patterns that are absolute or "
            "climb out with '..' (%s); a keep-list the sweep cannot honour could "
            "delete what it was meant to protect",
            target,
            ", ".join(unusable),
        )
        return SweepResult(SweepStatus.REFUSED)

    root = target.resolve()
    reason = _refusal_reason(root)
    if reason is not None:
        logger.warning(
            "Scratch sweep at worker start: refusing to sweep %s (resolved to %s): %s",
            target,
            root,
            reason,
        )
        return SweepResult(SweepStatus.REFUSED)

    if not root.is_dir():
        logger.info(
            "Scratch sweep at worker start: %s is not a directory, nothing to sweep",
            root,
        )
        return SweepResult(SweepStatus.NOTHING_TO_SWEEP)

    rules.extend(_temporary_file_directory_rule(root))
    tally = _Tally()
    _delete_children(root, rules, tally)

    logger.info(
        "Scratch sweep at worker start: root=%s removed_entries=%d "
        "removed_bytes=%d (%.2f GiB) kept=%d failed=%d",
        root,
        tally.removed_entries,
        tally.removed_bytes,
        tally.removed_bytes / _GIB,
        tally.kept_entries,
        tally.failed_entries,
    )
    if tally.first_failure is not None:
        error_type, path = tally.first_failure
        logger.warning(
            "Scratch sweep at worker start could not remove %d entries under %s "
            "(first: %s on %s); they stay on the volume",
            tally.failed_entries,
            root,
            error_type,
            path,
        )
    return SweepResult(
        SweepStatus.SWEPT,
        removed_entries=tally.removed_entries,
        removed_bytes=tally.removed_bytes,
        kept_entries=tally.kept_entries,
        failed_entries=tally.failed_entries,
    )


def _parse_keep(patterns: Iterable[str]) -> tuple[list[_Rule], list[str]]:
    """Turn keep patterns into rules, with :data:`BUILTIN_KEEP` first.

    Returns the rules and, separately, every pattern that cannot be honoured.
    Empty patterns are ignored; a leading ``./`` and trailing ``/`` are not
    significant.
    """
    rules: list[_Rule] = [tuple(pattern.split("/")) for pattern in BUILTIN_KEEP]
    unusable: list[str] = []
    for pattern in patterns:
        text = pattern.strip()
        if not text:
            continue
        segments = tuple(part for part in text.split("/") if part not in ("", "."))
        if text.startswith("/") or ".." in segments or not segments:
            unusable.append(text)
        else:
            rules.append(segments)
    return rules, unusable


def _refusal_reason(root: Path) -> str | None:
    """Why the resolved *root* must not be swept, or ``None`` when it may be."""
    depth = len(root.parts) - (1 if root.anchor else 0)
    if depth < 2:
        return (
            "it has fewer than two path components, so it is a filesystem root "
            "or a top-level directory"
        )
    home = _home_directory()
    if home is not None and _is_same_or_under(home, root):
        return "it is the home directory, or contains it"
    if _is_same_or_under(Path.cwd().resolve(), root):
        return "it is the working directory the app runs from, or contains it"
    return None


def _home_directory() -> Path | None:
    """The resolved home directory, or ``None`` when there is none to find.

    A container running under an arbitrary UID often has neither ``HOME`` nor a
    passwd entry, which leaves ``~`` unexpanded.
    """
    home = os.path.expanduser("~")
    if home == "~":
        return None
    return Path(home).resolve()


def _is_same_or_under(path: Path, directory: Path) -> bool:
    """Whether *path* is *directory* or lies beneath it."""
    return path == directory or directory in path.parents


def _temporary_file_directory_rule(root: Path) -> list[_Rule]:
    """An exact rule for the :mod:`tempfile` directory, when it lies inside *root*."""
    temporary_files = Path(tempfile.gettempdir()).resolve()
    if root not in temporary_files.parents:
        return []
    return [
        tuple(glob.escape(part) for part in temporary_files.relative_to(root).parts)
    ]


def _is_kept(relative: tuple[str, ...], rules: list[_Rule]) -> bool:
    """Whether a keep rule matches *relative* exactly, component for component."""
    return any(
        len(rule) == len(relative)
        and all(fnmatch.fnmatchcase(name, glob_) for name, glob_ in zip(relative, rule))
        for rule in rules
    )


@dataclass
class _Tally:
    """What the walk has done so far."""

    removed_entries: int = 0
    removed_bytes: int = 0
    kept_entries: int = 0
    failed_entries: int = 0
    #: ``(error type, path)`` of the first failure, for the one warning.
    first_failure: tuple[str, str] | None = None
    #: Directories, by relative path, that must outlive the walk because
    #: something beneath them was kept or could not be removed. Removing them
    #: could only fail, and would count that one failure twice.
    pinned: set[tuple[str, ...]] = field(default_factory=set)

    def keep(self, relative: tuple[str, ...]) -> None:
        """Record an entry left in place."""
        self.kept_entries += 1
        self._pin_parents(relative)

    def fail(
        self,
        path: str,
        relative: tuple[str, ...],
        exc: OSError,
        *,
        pin_self: bool = False,
    ) -> None:
        """Record an entry that could not be removed (or listed)."""
        self.failed_entries += 1
        if self.first_failure is None:
            self.first_failure = (type(exc).__name__, path)
        self._pin_parents(relative)
        if pin_self:
            self.pinned.add(relative)
        logger.debug("Scratch sweep could not remove %s: %s", path, type(exc).__name__)

    def _pin_parents(self, relative: tuple[str, ...]) -> None:
        self.pinned.update(relative[:depth] for depth in range(1, len(relative)))


def _delete_children(root: Path, rules: list[_Rule], tally: _Tally) -> None:
    """Delete everything under *root* that is not kept, deepest first.

    Iterative, so directory depth is bounded by memory rather than by the
    recursion limit. Each directory is listed once, then removed once its
    children are gone; *root* itself is never removed.
    """
    root_device = _device_of(str(root))
    # (path, path relative to root, whether its children have been handled)
    stack: list[tuple[str, tuple[str, ...], bool]] = [(str(root), (), False)]
    while stack:
        path, relative, emptied = stack.pop()
        if emptied:
            if relative and relative not in tally.pinned:
                _remove_directory(path, relative, tally)
            continue
        stack.append((path, relative, True))
        try:
            with os.scandir(path) as listing:
                entries = list(listing)
        except OSError as exc:
            tally.fail(path, relative, exc, pin_self=True)
            entries = []
        for entry in entries:
            entry_relative = (*relative, entry.name)
            if _is_kept(entry_relative, rules):
                tally.keep(entry_relative)
            elif not _is_directory(entry):
                _remove_file(entry, entry_relative, tally)
            elif _on_other_filesystem(entry.path, root_device):
                tally.keep(entry_relative)
            else:
                stack.append((entry.path, entry_relative, False))


def _is_directory(entry: os.DirEntry[str]) -> bool:
    """A real directory: not a link to one, and not a Windows junction."""
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    if is_junction is not None and is_junction():
        return False
    return entry.is_dir(follow_symlinks=False)


def _device_of(path: str) -> int:
    """The device *path* itself lives on, without following a link."""
    return os.lstat(path).st_dev


def _on_other_filesystem(path: str, root_device: int) -> bool:
    """Whether the directory at *path* is the mount point of another filesystem.

    A directory that can no longer be inspected counts as one: what cannot be
    identified is left alone rather than deleted.
    """
    try:
        return _device_of(path) != root_device
    except OSError as exc:
        logger.debug(
            "Scratch sweep could not inspect %s (%s); leaving it",
            path,
            type(exc).__name__,
        )
        return True


def _remove_file(
    entry: os.DirEntry[str], relative: tuple[str, ...], tally: _Tally
) -> None:
    """Unlink a file, link or other non-directory entry, counting its size."""
    try:
        size = _allocated_bytes(entry.stat(follow_symlinks=False))
        os.unlink(entry.path)
    except OSError as exc:
        tally.fail(entry.path, relative, exc)
    else:
        tally.removed_entries += 1
        tally.removed_bytes += size


def _remove_directory(path: str, relative: tuple[str, ...], tally: _Tally) -> None:
    """Remove a directory the walk has already emptied."""
    try:
        os.rmdir(path)
    except OSError as exc:
        tally.fail(path, relative, exc)
    else:
        tally.removed_entries += 1


def _allocated_bytes(stat: os.stat_result) -> int:
    """Disk space a file holds: allocated blocks where reported, size elsewhere.

    Blocks rather than size because a preallocated, partly written download
    claims its full size while holding far less.
    """
    blocks = getattr(stat, "st_blocks", None)
    return blocks * 512 if blocks is not None else stat.st_size
