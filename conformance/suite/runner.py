"""Conformance suite runner — dispatches registered checks, optionally filtered by series.

Usage::

    # Run everything
    python -m suite.runner --repo . --output report.sarif

    # Run only CI/workflow checks (C-series)
    python -m suite.runner --repo . --series C --output ci.sarif

    # Run multiple series
    python -m suite.runner --repo . --series E,L --output code.sarif
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from suite.checks import actions_pinning, error_handling
from suite.rules import get_rule
from suite.schema.disposition import EnforcementTier
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
    CheckRegistration(
        series=error_handling.SERIES,
        discover=error_handling.discover,
        scan_path=error_handling.scan_path,
    ),
]


def _tier(f: Finding) -> EnforcementTier:
    return get_rule(f.rule_id).tier


def _print_human_summary(
    findings: list[Finding],
    series: str | None,
    excluded_prefixes: tuple[str, ...] = (),
) -> None:
    """Print a human-readable violation/warning summary to stdout."""
    label = f"{series}-series" if series else "conformance suite"
    active = [f for f in findings if not f.suppressed]
    suppressed = [f for f in findings if f.suppressed]
    excluded_note = (
        f" (excluded: {', '.join(sorted(excluded_prefixes))})"
        if excluded_prefixes
        else ""
    )
    if not active and not suppressed:
        print(f"conformance ({label}): no violations found.{excluded_note}")
        return
    blocking = [f for f in active if _tier(f) == EnforcementTier.BLOCK]
    warnings = [f for f in active if _tier(f) == EnforcementTier.WARN]
    parts = []
    if blocking:
        parts.append(f"{len(blocking)} violation{'s' if len(blocking) != 1 else ''}")
    if warnings:
        parts.append(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}")
    if suppressed:
        parts.append(f"{len(suppressed)} suppressed")
    if not parts:
        print(f"conformance ({label}): no violations found.{excluded_note}")
        return
    print(f"conformance ({label}): {', '.join(parts)} found.{excluded_note}\n")
    for f in active:
        level = "FAIL" if _tier(f) == EnforcementTier.BLOCK else "WARN"
        print(f"  [{f.rule_id}] [{level}] {f.file}:{f.line}:{f.column}")
        print(f"  {f.message}\n")


def _pct(msg: str) -> str:
    """Percent-encode special characters per the GitHub Actions workflow-command spec."""
    return msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit_github_summary_annotations(
    findings: list[Finding],
    series: str | None,
    excluded_prefixes: tuple[str, ...] = (),
) -> None:
    """Emit at most four summary annotations to the GitHub Actions log.

    Per-finding annotations are capped at 10 per type by GitHub without any
    visible indication of truncation, making it impossible to track overall
    progress from the PR view.  Instead we emit one annotation per non-zero
    category (blocking / warning / suppressing), each carrying the full count
    and a link to the workflow run where the SARIF artifact can be downloaded.

    A fourth ``::notice`` is emitted when paths were excluded from scanning,
    so the reduced scope is always visible alongside the finding counts.

    Emits nothing when there are no findings and no exclusions.
    """
    blocking = [
        f for f in findings if not f.suppressed and _tier(f) == EnforcementTier.BLOCK
    ]
    warns = [
        f for f in findings if not f.suppressed and _tier(f) == EnforcementTier.WARN
    ]
    suppressed = [f for f in findings if f.suppressed]

    if not blocking and not warns and not suppressed and not excluded_prefixes:
        return

    server = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    repo = os.getenv("GITHUB_REPOSITORY", "")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    if repo and run_id:
        detail = f"Full report: {server}/{repo}/actions/runs/{run_id} (download the SARIF artifact)."
    else:
        detail = "Download the SARIF artifact from this workflow run for full details."

    label = f"{series}-series" if series else "conformance suite"

    if blocking:
        n = len(blocking)
        msg = _pct(
            f"{n} blocking violation{'s' if n != 1 else ''} found by {label}. {detail}"
        )
        print(
            f"::error title=Conformance: {n} blocking violation{'s' if n != 1 else ''} ({label})::{msg}"
        )

    if warns:
        n = len(warns)
        msg = _pct(f"{n} warning{'s' if n != 1 else ''} found by {label}. {detail}")
        print(
            f"::warning title=Conformance: {n} warning{'s' if n != 1 else ''} ({label})::{msg}"
        )

    if suppressed:
        n = len(suppressed)
        msg = _pct(
            f"{n} suppressed finding{'s' if n != 1 else ''} in {label}. {detail}"
        )
        print(f"::notice title=Conformance: {n} suppressed ({label})::{msg}")

    if excluded_prefixes:
        paths_str = ", ".join(sorted(excluded_prefixes))
        msg = _pct(
            f"{label} excluded from scanning: {paths_str}. "
            f"Findings in these paths are not counted. {detail}"
        )
        print(f"::notice title=Conformance: excluded paths ({label})::{msg}")


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
    parser.add_argument(
        "--exclude",
        metavar="PATHS",
        default="",
        help=(
            "Comma-separated repo-root-relative path prefixes to exclude from all "
            "checks, e.g. 'tools/,contract-toolkit/'.  Applies after discovery, so "
            "it works uniformly across every registered series."
        ),
    )
    args = parser.parse_args(argv)

    if args.series:
        requested = {s.strip().upper() for s in args.series.split(",")}
        active = [c for c in _CHECKS if c.series in requested]
    else:
        active = list(_CHECKS)

    # Build the set of excluded prefixes once (normalised, non-empty only).
    excluded_prefixes = tuple(
        p.strip().lstrip("/") for p in args.exclude.split(",") if p.strip()
    )

    root = Path(args.repo).resolve()
    all_findings: list[Finding] = []
    for check in active:
        for p in check.discover(root):
            if excluded_prefixes:
                rel = p.relative_to(root).as_posix()
                if any(rel.startswith(prefix) for prefix in excluded_prefixes):
                    continue
            all_findings.extend(check.scan_path(p, root))

    # Always surface violations in a human-readable form so CI logs are actionable.
    _print_human_summary(all_findings, args.series, excluded_prefixes)

    if os.getenv("GITHUB_ACTIONS") == "true":
        _emit_github_summary_annotations(all_findings, args.series, excluded_prefixes)

    report = findings_to_report(
        all_findings,
        tool_version=args.tool_version,
        excluded_paths=list(excluded_prefixes),
    )
    payload = json.dumps(report.model_dump(by_alias=True, exclude_none=True), indent=2)

    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    else:
        print(payload)

    return report.runs[0].invocations[0].exit_code  # type: ignore[return-value]


if __name__ == "__main__":
    sys.exit(main())
