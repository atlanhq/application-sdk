"""P-series worker-disk checks (P060–P062) — AST-based.

The disk model that lets a connector run on a small, growing worker volume: every
file the worker writes is removed once it has served its purpose, on every exit
path, and lives where the volume and the SDK's cleanup can see it.

* ``P060`` UploadedFileNotDeleted — an SDK ``upload_file`` / ``upload_prefix``
  whose local source is never removed after the upload (no
  ``retain_local_copy=False``, no ``os.remove`` / ``shutil.rmtree`` of it later
  in the function, ``finally`` included).
* ``P061`` TempFileOutsideWorkerVolume — a stdlib ``tempfile`` constructor given
  no directory (``dir=`` omitted, ``None``, ``gettempdir()`` or ``"/tmp"``), so
  its scratch lands in the system temp dir rather than under ``TEMPORARY_PATH``.
* ``P062`` TempScratchNotCleanedUp — ``tempfile.mkdtemp`` / ``mkstemp`` scratch
  that is not removed in a ``finally`` (or by a registered cleanup callback), so
  an exception leaves it behind.

All three resolve their targets through import bindings and follow a path
through the function it is used in (``conformance.suite.checks._local_files``),
so they are silent on app helpers that share a name and on paths a function
hands to its caller.

Discovery
---------
The standard source walk: ``tests/``, fixtures and infra directories are
excluded, so test scratch is never graded.

Inline suppression
------------------
``# conformance: ignore[P060] <reason>`` on the offending line, or on the
comment-only line directly above it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    discover,
    make_cli_main,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

from ._scratch_cleanup import check_p062
from ._tempfile_dir import check_p061
from ._upload_cleanup import check_p060

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan Python source *text* and return its P060–P062 findings."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return (
        check_p060(tree, file, directives)
        + check_p061(tree, file, directives)
        + check_p062(tree, file, directives)
    )


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file, producing a repo-root-relative URI."""
    text = safe_read_text(path)
    if text is None:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_text,
    description="P060-P062: worker-disk hygiene (upload cleanup, tempfile placement "
    "and scratch cleanup).",
)
"""CLI entry point for the worker-disk checks."""


if __name__ == "__main__":
    sys.exit(main())
