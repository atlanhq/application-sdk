"""Preflight conformance static checks.

Cross-file only: ``scan_path`` is a no-op and ``scan_all`` builds one shared
:class:`~._common.Registry` (single parse + import walk) then runs all rule
passes over it. Owns the ``F`` series and its leg of the fleet CI matrix.

Nothing here executes tests. F016 reads the scenario registrations under
``tests/`` statically; whether those tests pass is the test gate's measure.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conformance.suite.checks._ast_common import discover as discover_sources
from conformance.suite.checks._ast_common import make_cli_main
from conformance.suite.schema.findings import Finding

from . import (
    _contracts,
    _lifetime,
    _metadata_parity,
    _reserved_gate,
    _retired_suppression,
    _scenarios,
    _untyped_failure,
    _warning_log,
)
from ._common import SCENARIO_COVERAGE, build_registry, coverage_findings

SERIES = "F"

__all__ = ["SERIES", "discover", "main", "scan_all", "scan_path"]


def discover(root: Path) -> list[Path]:
    """App sources, plus the ``tests/`` modules F016 reads registrations from."""
    tests = root / "tests"
    scenario_modules = (
        [
            path
            for path in tests.rglob("*.py")
            if "__pycache__" not in path.parts
            and _scenarios.is_scenario_test_path(path.relative_to(root).as_posix())
        ]
        if tests.is_dir()
        else []
    )
    return sorted({*discover_sources(root), *scenario_modules})


def scan_path(path: Path, root: Path) -> list[Finding]:
    """No-op: the preflight checks need the whole repo; use :func:`scan_all`."""
    return []


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Run the static preflight passes over *paths*."""
    test_paths = [
        p
        for p in paths
        if _scenarios.is_scenario_test_path(p.relative_to(root).as_posix())
    ]
    reg = build_registry([p for p in paths if p not in set(test_paths)], root)
    findings: list[Finding] = []
    findings.extend(_reserved_gate.scan(reg))
    findings.extend(_untyped_failure.scan(reg))
    findings.extend(_metadata_parity.scan(reg))
    findings.extend(_warning_log.scan(reg))
    findings.extend(_contracts.scan(reg))
    findings.extend(_lifetime.scan(reg))
    findings.extend(_retired_suppression.scan(reg))
    findings.extend(coverage_findings(reg))
    scenario_findings = _scenarios.scan(reg, build_registry(test_paths, root))
    findings.extend(scenario_findings)
    # A value-level F019 gap names a property every registered F016 scenario
    # asserts through assert_preflight_result. With the matrix fully defined,
    # the test gate is what proves those assertions hold, so the gap is closed
    # here. Suppressed F016 findings still count as gaps: a suppression cannot
    # manufacture a complete matrix.
    if _scenarios.declares_preflight(reg) and not scenario_findings:
        findings = [
            f
            for f in findings
            if not (f.cleared_by and f.cleared_by <= SCENARIO_COVERAGE)
        ]
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
