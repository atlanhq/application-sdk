"""P-series run-scratch check — AST-based (FND-5364).

* ``P067`` TempDirOutsideRunScratch (app) — ``tempfile.gettempdir()``, or
  ``tempfile.mkdtemp`` / ``mkstemp`` with no directory, in app code.  The
  workflow sandbox refuses ``tempfile``, ``cleanup_files`` never removes the
  system temp dir, and a ``/tmp`` path has no object-store mapping;
  ``App.run_scratch_dir`` is the per-run directory the SDK owns.

Registered under series letter ``P`` alongside the other seam checks.  Uses
the shared discovery walk, which excludes ``tests/`` dirs: a test's temp dir
is pytest's to manage.

SDK-version gate
----------------
The fix uses ``App.run_scratch_dir``, which exists only from
:data:`RUN_SCRATCH_SDK_FLOOR`.  :func:`scan_all` and :func:`scan_path` read
the SDK version the repo's ``uv.lock`` resolves and return nothing unless it
is confirmed at or above the floor; no lock, no SDK in it, or an unparseable
version all stay silent.  :func:`scan_text` is the pure AST pass with no repo
to read, so it is ungated.

Inline suppression
------------------
Add ``# conformance: ignore[P067] <reason>`` on the offending line, or on the
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
    read_source,
)
from conformance.suite.checks._version import locked_sdk_reaches
from conformance.suite.schema.findings import Finding

from ._detect import check_p067

SERIES = "P"

#: First application-sdk release that ships ``App.run_scratch_dir``
#: (FND-5364).  Silent below it: prescribing an API the app's SDK does not
#: have is a finding nobody can act on.
RUN_SCRATCH_SDK_FLOOR = (3, 59, 0)

__all__ = [
    "RUN_SCRATCH_SDK_FLOOR",
    "SERIES",
    "discover",
    "main",
    "scan_all",
    "scan_path",
    "scan_text",
]


def sdk_has_run_scratch_dir(root: Path) -> bool:
    """True iff ``root/uv.lock`` resolves an SDK at or above the floor."""
    return locked_sdk_reaches(root, RUN_SCRATCH_SDK_FLOOR)


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan one source *text* for P067 (ungated)."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    return check_p067(tree, file, _parse_directives(text))


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan one Python file, gated on *root*'s locked SDK version."""
    if not sdk_has_run_scratch_dir(root):
        return []
    source = read_source(path, root)
    return scan_text(*source) if source is not None else []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan every discovered file (gated once on the repo's SDK version)."""
    if not sdk_has_run_scratch_dir(root):
        return []
    findings: list[Finding] = []
    for path in paths:
        source = read_source(path, root)
        if source is not None:
            findings.extend(scan_text(*source))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "Run-scratch P-series check (P067): scan Python files for "
        "tempfile.gettempdir() and mkdtemp/mkstemp with no dir=, which put run "
        "files outside App.run_scratch_dir (silent below the SDK floor)."
    ),
)
"""CLI entry point for the run-scratch check."""


if __name__ == "__main__":
    sys.exit(main())
