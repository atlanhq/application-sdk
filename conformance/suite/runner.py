"""Conformance suite runner — dispatches registered checks, optionally filtered by series.

Usage::

    # Run everything
    python -m suite.runner --repo . --output report.sarif

    # Run only CI/workflow checks (C-series)
    python -m suite.runner --repo . --series C --output ci.sarif

    # Run multiple series
    python -m suite.runner --repo . --series P,L --output code.sarif
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from suite.checks import actions_pinning
from suite.schema.findings import Finding, findings_to_report


@dataclass(frozen=True)
class CheckRegistration:
    """A registered check module, identified by its rule-series letter."""

    series: str
    discover: Callable[[Path], list[Path]]
    scan_path: Callable[[Path, Path], list[Finding]]


_CHECKS: list[CheckRegistration] = [
    CheckRegistration(
        series=actions_pinning.SERIES,
        discover=actions_pinning.discover,
        scan_path=actions_pinning.scan_path,
    ),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Atlan conformance suite runner.")
    parser.add_argument("--repo", default=".", metavar="DIR")
    parser.add_argument(
        "--output", metavar="FILE", help="Write SARIF to FILE (default: stdout)"
    )
    parser.add_argument("--tool-version", default="3.16.0", metavar="VERSION")
    parser.add_argument(
        "--series",
        metavar="LETTERS",
        help=(
            "Comma-separated series letters to run, e.g. 'C' or 'P,L'. "
            "Default: all registered checks."
        ),
    )
    args = parser.parse_args(argv)

    if args.series:
        requested = {s.strip().upper() for s in args.series.split(",")}
        active = [c for c in _CHECKS if c.series in requested]
    else:
        active = list(_CHECKS)

    root = Path(args.repo).resolve()
    all_findings: list[Finding] = []
    for check in active:
        for p in check.discover(root):
            all_findings.extend(check.scan_path(p, root))

    report = findings_to_report(all_findings, tool_version=args.tool_version)
    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)

    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    return report.runs[0].invocations[0].exit_code  # type: ignore[return-value]


if __name__ == "__main__":
    sys.exit(main())
