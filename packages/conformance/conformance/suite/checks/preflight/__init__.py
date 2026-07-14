"""Preflight-gate checks (P032–P035, BLDX-1545).

Cross-file only: ``scan_path`` is a no-op and ``scan_all`` builds one shared
:class:`~._common.Registry` (single parse + import walk) then runs all four rule
passes over it. Reuses the ``P`` series so it runs on the existing P leg of the
fleet CI matrix with no workflow change.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conformance.suite.checks._ast_common import discover, make_cli_main
from conformance.suite.schema.findings import Finding

from . import _metadata_parity, _reserved_gate, _untyped_failure
from ._common import build_registry

SERIES = "P"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def scan_path(path: Path, root: Path) -> list[Finding]:  # noqa: ARG001
    """No-op: the preflight checks need the whole repo; use :func:`scan_all`."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Run the P032–P035 preflight-gate passes over *paths*."""
    reg = build_registry(paths, root)
    findings: list[Finding] = []
    findings.extend(_reserved_gate.scan(reg))
    findings.extend(_untyped_failure.scan(reg))
    findings.extend(_metadata_parity.scan(reg))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "Preflight-gate conformance (P032-P035): reserved gate-name collision, "
        "duplicate in-workflow preflight, untyped check failures, and "
        "metadata/input-contract parity (BLDX-1545)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
