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
import os
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


def _print_human_summary(findings: list[Finding], series: str | None) -> None:
    """Print a human-readable violation summary to stdout."""
    label = f"{series}-series" if series else "conformance suite"
    n = len(findings)
    if not n:
        print(f"conformance ({label}): no violations found.")
        return
    print(f"conformance ({label}): {n} violation{'s' if n != 1 else ''} found.\n")
    for f in findings:
        print(f"  [{f.rule_id}] {f.file}:{f.line}:{f.column}")
        print(f"  {f.message}\n")


def _emit_github_annotations(findings: list[Finding]) -> None:
    """Emit GitHub Actions ::error workflow commands for inline PR annotations."""
    for f in findings:
        # Percent-encode special characters per the GitHub Actions docs.
        msg = f.message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(
            f"::error file={f.file},line={f.line},col={f.column},"
            f"title={f.rule_id}::{msg}"
        )


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

    # Always surface violations in a human-readable form so CI logs are actionable.
    _print_human_summary(all_findings, args.series)

    # In GitHub Actions, also emit ::error annotations so violations appear
    # as inline comments on the PR's Files Changed view.
    if os.getenv("GITHUB_ACTIONS") == "true":
        _emit_github_annotations(all_findings)

    report = findings_to_report(all_findings, tool_version=args.tool_version)
    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)

    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    return report.runs[0].invocations[0].exit_code  # type: ignore[return-value]


if __name__ == "__main__":
    sys.exit(main())
