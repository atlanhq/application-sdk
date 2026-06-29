"""P-series client-seam check — AST-based (BLDX-1430).

Enforces that Atlan services are reached through ``pyatlan`` (exposed by the SDK
via ``application_sdk.credentials``), not hand-rolled raw HTTP:

* ``P019`` RawHttpToAtlan (both) — a raw ``httpx``/``requests``/``aiohttp``/``urllib``
  request whose URL statically names an Atlan service path (``/api/meta`` = Atlas,
  ``/api/service`` = Heracles).

This is a third check registered under series letter ``P`` (alongside
``prescriptions`` and ``orchestration``), the established multi-module pattern.

Discovery note
--------------
Unlike the orchestration series, this check uses the **shared** discovery walk,
which excludes ``tests/`` / ``test/`` dirs (and infra dirs) but **not** the
shipped ``testing/`` subpackage.  So the SDK's e2e harness
(``application_sdk/testing/e2e/client.py``), which makes intentional low-level
``urllib`` calls to Atlan, *is* scanned — it stays silent only because it builds
its request URL from variables, which the heuristic deliberately does not match
(see ``_raw_http_to_atlan``).  A harness that hit Atlan with a *literal* URL
would fire P019 (WARN); the right tool there is an inline
``# conformance: ignore[P019] <reason>``.

Inline suppression
------------------
Add ``# conformance: ignore[P019] <reason>`` on the offending line (or the
comment-only line directly above it).
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
from conformance.suite.schema.findings import Finding

from ._raw_http_to_atlan import check_p019

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for the client-seam findings (P019)."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return check_p019(tree, file, directives)


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file, producing repo-root-relative URIs."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


main = make_cli_main(
    scan_text,
    description="Client-seam P-series check (P019): scan Python files for raw HTTP to Atlan.",
)
"""CLI entry point for the client-seam check."""


if __name__ == "__main__":
    sys.exit(main())
