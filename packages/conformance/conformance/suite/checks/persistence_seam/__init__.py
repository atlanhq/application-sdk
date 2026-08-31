"""P-series persistence-seam check — AST-based (CONNECT-1275).

Enforces that a connection's persistent-artifacts object-store layout is derived
from the SDK rather than assembled by the app:

* ``P048`` AppDerivedPersistentArtifactPrefix (app) — app code assembling the
  connection-scoped layout ``persistent-artifacts/apps/<app>/connection/…``
  instead of deriving it from ``get_persistent_s3_prefix``.
* ``P049`` StrictConnectionQualifiedNameParse (app) — a function that parses a
  ``connection_qualified_name`` itself and raises on it, where the SDK warns and
  proceeds, and that does not delegate the parse to the SDK's
  ``get_persistent_s3_prefix`` / ``extract_epoch_id_from_qualified_name``.

This is a fourth check registered under series letter ``P`` (alongside
``prescriptions``, ``orchestration`` and ``client_seam``), the established
multi-module pattern.

Scope note
----------
``P048`` is ``app``-scoped and the runner filters out-of-scope findings before
they reach the report, so this check needs no self-exemption guard: the SDK's
own modules define the layout and would otherwise flag themselves.

Inline suppression
------------------
Add ``# conformance: ignore[P048] <reason>`` or ``# conformance: ignore[P049]
<reason>`` on the offending line (or the comment-only line directly above it).
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

from ._derived_persistent_prefix import check_p048
from ._strict_qualified_name_parse import check_p049

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_path", "scan_text"]


def scan_text(text: str, file: str) -> list[Finding]:
    """Scan a single Python source *text* for the persistence-seam findings (P048/P049)."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return [
        *check_p048(tree, file, directives),
        *check_p049(tree, file, directives),
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
    description=(
        "Persistence-seam P-series check (P048/P049): scan Python files for "
        "app-built persistent-artifacts object-store paths and app-side "
        "strict parsing of connection qualified names."
    ),
)
"""CLI entry point for the persistence-seam check."""


if __name__ == "__main__":
    sys.exit(main())
