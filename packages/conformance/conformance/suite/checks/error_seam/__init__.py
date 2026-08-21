"""P-series error-seam checks — AST-based (CONNECT-970).

Enforces that an app depends on the SDK's **public** error contract,
``application_sdk.errors.__all__``, and not on an error class from an internal
module that can be reorganised without a deprecation cycle:

* ``P043`` NonPublicErrorControlFlow (app) — ``except`` / ``isinstance`` /
  ``issubclass`` / subclassing on an SDK error class the public surface does not
  export.  This is the defect: when the SDK changes which class a boundary
  surfaces, a sibling class silently stops matching and the guard becomes dead
  code.
* ``P045`` PrivateErrorClassImport (app) — importing such a class at all.  This
  is the coupling that makes P043 possible.

Scoped to ``application_sdk.storage.formats.*`` for now; see
``_public_error_surface`` for why, and for how to widen it.

This is a fourth check registered under series letter ``P`` (alongside
``prescriptions``, ``orchestration`` and ``client_seam``), the established
multi-module pattern.

Discovery note
--------------
Like the orchestration series, this series **includes test files**.  The
motivating incident had the superseded exception shape hand-built into a unit
test fixture, which then passed forever and hid the break; a rule that skipped
tests would have missed it.

Inline suppression
------------------
Add ``# conformance: ignore[P043] <reason>`` (or ``P045``) on the offending line,
or on the comment-only line directly above it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    EXCLUDE_DIRS,
    _parse_directives,
    make_cli_main,
)
from conformance.suite.schema.findings import Finding

from ._catch_site import check_p043
from ._private_error_import import check_p044

SERIES = "P"

# This series scans test files too (see module docstring), so the universal
# ``tests``/``test`` directory exclusion is dropped from the discovery filter.
_EXCLUDE_DIRS = EXCLUDE_DIRS - {"tests", "test"}

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def discover(root: Path) -> list[Path]:
    """Discover Python sources under *root*, **including** test files.

    Mirrors the shared discovery walk (skip infra/virtualenv/dot dirs) but keeps
    the ``tests``/``test`` trees — a test that freezes a superseded exception
    shape into a fixture is exactly the case to catch.
    """
    paths: list[Path] = []
    for path in root.rglob("*.py"):
        if set(path.parts) & _EXCLUDE_DIRS:
            continue
        rel_parts = path.relative_to(root).parts
        if any(p.startswith(".") for p in rel_parts[:-1]):
            continue
        paths.append(path)
    return sorted(paths)


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for the error-seam findings (P043, P045)."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return [
        *check_p043(tree, file, directives),
        *check_p044(tree, file, directives),
    ]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file, producing repo-root-relative URIs."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_text,
    description="Error-seam P-series checks (P043/P045): scan Python files.",
    discover=discover,
)


if __name__ == "__main__":
    sys.exit(main())
