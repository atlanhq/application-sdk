"""P-series offload-cancel check — AST-based (FND-4707).

Flags offloaded work that a cancel cannot stop:

* ``P064`` UncancellableOffloadedCursorStatement (app) — a ``run_in_thread``
  call runs a DB-API cursor statement (``<cursor>.execute`` /
  ``executemany`` on a cursor from ``<conn>.cursor()``) and registers no driver
  cancel for it.
* ``P065`` BatchedResultsIteratedOnLoop (app) — inside an ``async def``, the
  result of ``await <client>.get_batched_results(...)`` is iterated with a sync
  ``for`` (or a sync comprehension) instead of ``async for``.

Registered under series letter ``P`` alongside the other seam checks.

SDK-version gate
----------------
Both fixes use SDK surfaces that exist only from :data:`OFFLOAD_CANCEL_SDK_FLOOR`:
``current_cancel_handle()`` (a handle on every ``run_in_thread`` call) and a
``get_batched_results`` result that supports ``async for``.  :func:`scan_all`
and :func:`scan_path` read the SDK version the repo's ``uv.lock`` resolves and
return nothing unless it is confirmed at or above the floor; no lock, no SDK in
it, or an unparseable version all stay silent.  :func:`scan_text` is the pure
AST pass with no repo to read, so it is ungated.

Inline suppression
------------------
Add ``# conformance: ignore[P064] <reason>`` (or ``P065``) on the offending
line, or on the comment-only line directly above it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    discover,
    make_cli_main,
)
from conformance.suite.checks._version import (
    locked_sdk_version,
    parse_version,
    version_reached,
)
from conformance.suite.schema.findings import Finding

from ._detect import check_p064, check_p065

SERIES = "P"

#: First application-sdk release that ships both fixes (application-sdk#4766 and
#: #4768): a ``CancelHandle`` on every ``run_in_thread`` call, reachable with
#: ``current_cancel_handle()``, and a ``get_batched_results`` result that is also
#: an async iterator.  Silent below it.
OFFLOAD_CANCEL_SDK_FLOOR = (3, 56, 0)

__all__ = [
    "OFFLOAD_CANCEL_SDK_FLOOR",
    "SERIES",
    "discover",
    "main",
    "scan_all",
    "scan_path",
    "scan_text",
]


def sdk_has_offload_cancel(root: Path) -> bool:
    """True iff ``root/uv.lock`` resolves an SDK at or above the floor.

    Fails closed: prescribing an API the app's SDK does not have is a finding
    nobody can act on.
    """
    locked = locked_sdk_version(root)
    if locked is None:
        return False
    parsed = parse_version(locked)
    return parsed is not None and version_reached(OFFLOAD_CANCEL_SDK_FLOOR, parsed)


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan one source *text* for P064/P065 (ungated)."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return [*check_p064(tree, file, directives), *check_p065(tree, file, directives)]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan one Python file, gated on *root*'s locked SDK version."""
    if not sdk_has_offload_cancel(root):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)
    return scan_text(text, rel)


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan every discovered file (gated once on the repo's SDK version)."""
    if not sdk_has_offload_cancel(root):
        return []
    findings: list[Finding] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        findings.extend(scan_text(text, rel))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "Offload-cancel P-series check (P064/P065): scan Python files for "
        "run_in_thread cursor statements with no driver cancel, and "
        "get_batched_results iterated with a sync for in an async def (silent "
        "below the SDK floor)."
    ),
)
"""CLI entry point for the offload-cancel check."""


if __name__ == "__main__":
    sys.exit(main())
