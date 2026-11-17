"""The per-file ``scan_path`` entry point most check series share.

A series that scans one file at a time exposes ``scan_text(text, uri, …)`` and
a ``scan_path(path, root)`` the runner calls per discovered file.  The latter
was the same ten lines in two dozen modules: read the file defensively, skip it
when unreadable, report it by its *root*-relative path.  :func:`make_scan_path`
builds it once, so the read guard and the URI rule cannot drift between series.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from conformance.suite.schema.findings import Finding

from ._io import safe_read_text

__all__ = ["make_scan_path", "read_source", "relative_uri"]

#: ``scan_text(text, uri, **context) -> findings``.
ScanText = Callable[..., list[Finding]]
#: Builds the extra ``scan_text`` keywords from ``(path, root)``.
ScanContext = Callable[[Path, Path], Mapping[str, object]]


def relative_uri(path: Path, root: Path) -> str:
    """*path* relative to *root* as an OS-native string, or *path* itself when outside it."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def read_source(path: Path, root: Path) -> tuple[str, str] | None:
    """``(text, uri)`` for *path*, or ``None`` when it cannot be read or decoded."""
    text = safe_read_text(path)
    if text is None:
        return None
    return text, relative_uri(path, root)


def make_scan_path(
    scan_text: ScanText, *, context: ScanContext | None = None
) -> Callable[[Path, Path], list[Finding]]:
    """Build ``scan_path(path, root)`` around a series' ``scan_text``.

    The built function reads *path* (an unreadable or undecodable file yields
    no findings) and calls ``scan_text(text, uri)`` with *uri* relative to
    *root*.  *context*, when given, is called with ``(path, root)`` only after
    the read succeeds, and its mapping is passed to ``scan_text`` as keywords:
    the hook for a series whose findings depend on the rest of the repo.
    """

    def scan_path(path: Path, root: Path) -> list[Finding]:
        source = read_source(path, root)
        if source is None:
            return []
        text, uri = source
        if context is None:
            return scan_text(text, uri)
        return scan_text(text, uri, **context(path, root))

    # Name it as the module attribute it is bound to, not as a closure.
    scan_path.__qualname__ = "scan_path"
    return scan_path
