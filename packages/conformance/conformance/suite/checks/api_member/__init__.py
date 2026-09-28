"""P-series hosted API member check — P054 HostedApiMemberNotThin (FND-2964).

An app hosted on the consolidated API server declares
``[project.entry-points."atlan.app_api"] <name> = "<pkg>:handler"`` (normally in
``api/pyproject.toml``); the server imports ``<pkg>`` into a process shared with
every other hosted app and without ``atlan-application-sdk``.  P054 grades that
package: no ``application_sdk`` / ``app`` imports, no import-time environment
reads, and an entry-point name equal to the app's name.

**Not evaluated** unless some ``pyproject.toml`` declares the entry point: with
none, :func:`scan_all` returns ``[]`` without reading a source file, so a repo
that has not adopted hosting can never see a P054 finding.

Inline suppression
------------------
``# conformance: ignore[P054] <reason>`` on the offending source line, or on the
entry-point line of ``pyproject.toml`` for the name sub-check.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conformance.suite.checks._ast_common import EXCLUDE_DIRS
from conformance.suite.checks._ast_common import discover as _discover_sources
from conformance.suite.checks._ast_common import make_cli_main
from conformance.suite.schema.findings import Finding

from ._check import ENTRY_POINT_GROUP, RULE_ID, entry_points, scan

SERIES = "P"

__all__ = [
    "ENTRY_POINT_GROUP",
    "RULE_ID",
    "SERIES",
    "discover",
    "entry_points",
    "main",
    "scan_all",
    "scan_path",
]


def discover(root: Path) -> list[Path]:
    """Every ``pyproject.toml`` (skipping infra and dot dirs) plus the Python sources."""
    pyprojects: list[Path] = []
    for path in root.rglob("pyproject.toml"):
        dir_parts = path.relative_to(root).parts[:-1]
        if set(dir_parts) & EXCLUDE_DIRS:
            continue
        if any(p.startswith(".") for p in dir_parts):
            continue
        pyprojects.append(path)
    return sorted(pyprojects) + _discover_sources(root)


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Run P054 over *paths* (see :func:`~._check.scan`)."""
    return scan(paths, root)


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: P054 needs the entry-point declaration; use :func:`scan_all`."""
    return []


main = make_cli_main(
    scan_all=scan_all,
    description="P054: grade a hosted atlan.app_api member for isolation.",
    discover=discover,
    default_scan_paths=(".",),
)


if __name__ == "__main__":
    sys.exit(main())
