"""Preflight conformance static checks and optional behavioral scenarios.

Cross-file only: ``scan_path`` is a no-op and ``scan_all`` builds one shared
:class:`~._common.Registry` (single parse + import walk) then runs all rule
passes over it. Owns the ``F`` series and its leg of the fleet CI matrix.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conformance.suite.checks._ast_common import discover, make_cli_main
from conformance.suite.schema.findings import Finding

from . import (
    _contracts,
    _lifetime,
    _metadata_parity,
    _reserved_gate,
    _untyped_failure,
    _warning_log,
)
from ._common import build_registry, coverage_findings

SERIES = "F"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """No-op: the preflight checks need the whole repo; use :func:`scan_all`."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Run the static preflight passes over *paths*."""
    reg = build_registry(paths, root)
    findings: list[Finding] = []
    findings.extend(_reserved_gate.scan(reg))
    findings.extend(_untyped_failure.scan(reg))
    findings.extend(_metadata_parity.scan(reg))
    findings.extend(_warning_log.scan(reg))
    findings.extend(_contracts.scan(reg))
    findings.extend(_lifetime.scan(reg))
    findings.extend(coverage_findings(reg))
    return findings


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "Preflight-gate conformance (F-series): gate-name collision, duplicate "
        "preflight, typed failures, contract parity, actionable verdicts, probe "
        "lifetime and safe output (BLDX-1545, CONNECT-812)."
    ),
)

if __name__ == "__main__":
    sys.exit(main())
