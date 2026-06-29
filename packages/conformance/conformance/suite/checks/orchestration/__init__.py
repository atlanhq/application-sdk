"""P-series orchestration-seam checks — AST-based (BLDX-1417).

Enforces the single-seam contract between consumer apps and the Temporal
orchestration engine: apps reach orchestration only through the SDK seam, and the
SDK keeps Temporal contained behind that seam.

* ``P004`` DirectTemporalImport (app) — a direct ``temporalio`` import; apps must
  use ``application_sdk.app`` / ``application_sdk.execution``.
* ``P005`` PrivateOrchestrationInternalImport (app) — an import that reaches past
  the public seam into SDK-private internals (``application_sdk.…._temporal.…`` and
  other ``_``-prefixed modules).
* ``P006`` TemporalImportOutsideAdapter (sdk) — a ``temporalio`` import outside the
  ``execution/_temporal`` adapter (and the curated ``app/__init__`` primitive seam).
* ``P007`` RawTemporalInPublicSurface (sdk) — a public API that re-exports a raw
  ``temporalio`` symbol or exposes a raw Temporal type in a signature. Cross-file.

Discovery note
--------------
Unlike the other AST series, this series **includes test files**.  The motivating
violation class — an integration harness wiring Temporal directly instead of
through the SDK seam (e.g. ``tests/integration/conftest.py``) — lives in the test
tree, and a test harness is app code that must still honour the seam.  The
SDK-scope rules (P006/P007) individually exempt test files (see
``_temporal_common.is_test_file``); the app-scope rules (P004/P005) do not.

Inline suppression
------------------
Add ``# conformance: ignore[PXXX] <reason>`` on the offending line (or the
comment-only line directly above it).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from conformance.suite.checks._ast_common import (
    EXCLUDE_DIRS,
    _IgnoreDirective,
    _parse_directives,
    make_cli_main,
)
from conformance.suite.schema.findings import Finding

from ._direct_temporal_import import check_p004
from ._private_orchestration_import import check_p005
from ._public_temporal_surface import emit_p007
from ._temporal_import_confinement import check_p006

SERIES = "P"

# This series scans test files too (see module docstring), so the universal
# ``tests``/``test`` directory exclusion is dropped from the discovery filter.
_EXCLUDE_DIRS = EXCLUDE_DIRS - {"tests", "test"}

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path", "scan_text"]


def discover(root: Path) -> list[Path]:
    """Discover Python sources under *root*, **including** test files.

    Mirrors the shared discovery walk (skip infra/virtualenv/dot dirs) but keeps
    the ``tests``/``test`` trees and ``test_*`` files — the orchestration seam
    applies to test harnesses too.
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
    """Scan a single Python source *text* for the per-file P-series findings.

    Covers P004, P005 and P006.  P007 needs cross-file context; use
    :func:`scan_all` for full-suite runs.
    """
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    return [
        *check_p004(tree, file, directives),
        *check_p005(tree, file, directives),
        *check_p006(tree, file, directives),
    ]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan a single Python file (P004 + P005 + P006).  P007 requires :func:`scan_all`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan *paths*, emitting P004 + P005 + P006 (per-file) and P007 (cross-file)."""
    findings: list[Finding] = []
    p007_files: list[tuple[str, ast.Module]] = []
    directives_by_rel: dict[str, dict[int, _IgnoreDirective]] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        rel_str = str(rel)
        directives = _parse_directives(text)
        if isinstance(tree, ast.Module):
            p007_files.append((rel_str, tree))
            directives_by_rel[rel_str] = directives

        findings.extend(check_p004(tree, rel_str, directives))
        findings.extend(check_p005(tree, rel_str, directives))
        findings.extend(check_p006(tree, rel_str, directives))

    findings.extend(emit_p007(p007_files, directives_by_rel))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    description="Orchestration-seam P-series checks (P004-P007): scan Python files.",
)


if __name__ == "__main__":
    sys.exit(main())
