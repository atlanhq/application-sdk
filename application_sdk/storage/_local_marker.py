"""The local ``{path}.sha256`` marker: a verified digest plus a stat fingerprint.

A local marker records what a file held when the SDK last verified it — after
a persist uploaded it, or a materialise downloaded or re-hashed it. Its one
line is::

    <sha256 hex> <size> <mtime_ns> <inode>

The fingerprint is what lets a same-pod hand-off reuse a multi-GB file without
reading it: if the file's ``(size, mtime_ns, inode)`` still match, its content
is trusted to still be the recorded digest (FND-4081). Any write to the file
moves its mtime, a truncate or append moves its size, and an ``os.replace``
over it moves its inode, so each of those falls back to a full re-hash. A
replacement that also restores size *and* mtime on the same inode is not
caught — the same trade ``download_prefix(sync=True)`` makes in
``batch._plan_sync``.

A marker written before the fingerprint existed holds only the digest. It is
still read, but never trusted on stat alone: the caller re-hashes (off the
event loop) and re-stamps it in the current format.

Local markers are SDK bookkeeping on one pod's disk, never uploaded (uploads
and listings skip ``*.sha256``) and not read by any other repo, so the format
is free to change.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from application_sdk.storage.integrity import SIDECAR_SUFFIX, _is_sha256_hex

#: ``(size, mtime_ns, inode)`` of a regular file.
Fingerprint = tuple[int, int, int]


@dataclass(frozen=True)
class LocalMarker:
    """A parsed local marker."""

    sha256: str
    fingerprint: Fingerprint | None
    """``None`` for a digest-only marker written before FND-4081."""


def marker_path(local_path: str) -> str:
    """Path of the local marker beside *local_path*."""
    return local_path + SIDECAR_SUFFIX


def file_fingerprint(path: str) -> Fingerprint | None:
    """``(size, mtime_ns, inode)`` of a regular file at *path*, ``None`` otherwise.

    A symlink is not a regular file here (``follow_symlinks=False``, as in
    ``batch._regular_file_stat``): its target can change without the link's
    own stat moving, so a symlinked path is never trusted on stat alone.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def format_marker(sha256: str, fingerprint: Fingerprint | None) -> str:
    """Serialise a marker; a missing fingerprint writes the digest-only form."""
    if fingerprint is None:
        return sha256
    size, mtime_ns, inode = fingerprint
    return f"{sha256} {size} {mtime_ns} {inode}"


def parse_marker(text: str) -> LocalMarker | None:
    """Parse marker *text*; ``None`` if it is not a marker this SDK wrote."""
    parts = text.split()
    if not parts or not _is_sha256_hex(parts[0]):
        return None
    digest = parts[0].lower()
    if len(parts) == 1:
        return LocalMarker(digest, None)
    if len(parts) != 4 or not all(p.isdigit() for p in parts[1:]):
        return None
    size, mtime_ns, inode = (int(p) for p in parts[1:])
    return LocalMarker(digest, (size, mtime_ns, inode))


def read_local_marker(local_path: str) -> LocalMarker | None:
    """The marker beside *local_path*, or ``None`` if absent or unreadable."""
    try:
        with open(marker_path(local_path), encoding="utf-8") as fh:
            # A marker is one short line; cap the read so a stray large file
            # at the marker's name cannot cost a full read.
            text = fh.read(256)
    except OSError:
        return None
    return parse_marker(text)


def trusted_digest(local_path: str, *, size_bytes: int | None = None) -> str | None:
    """The digest of *local_path* if its marker can be trusted without hashing.

    Trusted means: the marker carries a fingerprint, the file's current
    ``(size, mtime_ns, inode)`` equals it, and — when the caller knows the
    size the producer recorded (``FileReference.size_bytes``) — that size
    agrees too. Anything else returns ``None`` and the caller must hash.
    """
    marker = read_local_marker(local_path)
    if marker is None or marker.fingerprint is None:
        return None
    current = file_fingerprint(local_path)
    if current is None or current != marker.fingerprint:
        return None
    if size_bytes is not None and current[0] != size_bytes:
        return None
    return marker.sha256
