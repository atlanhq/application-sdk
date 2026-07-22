"""``scorecard`` subcommand — emit a test-readiness scorecard JSON.

Reads the standard test-evidence artifacts a CI run already produces (pytest
junit XML + coverage.py JSON) and scores them against the bundled rubric.

Post tier-split, unit and integration run as separate CI jobs, so evidence
arrives per-tier: a dedicated aggregation job downloads both jobs' artifacts and
invokes this with per-tier flags.  unit + integration are always scored (a
missing junit → an empty, zero-scored tier, so a missing integration suite
still counts against the grade); e2e is scored only when ``--e2e-junit`` is
supplied — otherwise the e2e tier is marked not-applicable.

This is the one impure edge: it touches the filesystem and stamps
``generatedAt``.  All scoring logic lives in the pure ``readers`` + ``compute``.

Usage::

    atlan-application-sdk-conformance scorecard \\
        --unit-junit unit/results/test-results.xml \\
        --unit-coverage unit/coverage.json \\
        --integration-junit integration/results/test-results.xml \\
        --integration-coverage integration/coverage.json \\
        --repo "$GITHUB_REPOSITORY" --commit "$GITHUB_SHA" \\
        --out results/test-readiness.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path

from conformance.scorecard.compute import build_scorecard
from conformance.scorecard.readers import parse_coverage_json, parse_junit_tier
from conformance.scorecard.rubric import load_rubric
from conformance.scorecard.schema import (
    CoverageMetrics,
    RawTests,
    TierName,
    TierTestCounts,
)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _app_from_repo(repo: str) -> str:
    """Derive a short app name from ``owner/atlan-<app>-app`` (best effort)."""
    name = repo.split("/")[-1]
    if name.startswith("atlan-") and name.endswith("-app"):
        return name[len("atlan-") : -len("-app")]
    return name


def _counts(path: str | None) -> TierTestCounts:
    """Parse a tier's junit into counts; empty (zeroed) when absent."""
    if path and Path(path).exists():
        return parse_junit_tier(path)
    if path:
        print(f"warning: junit not found, tier scored empty: {path}")
    return TierTestCounts()


def _coverage(path: str | None) -> CoverageMetrics | None:
    if path and Path(path).exists():
        return parse_coverage_json(path)
    if path:
        print(f"warning: coverage file not found, scoring tier without it: {path}")
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="atlan-application-sdk-conformance scorecard",
        description="Emit a test-readiness scorecard from per-tier junit + coverage.",
    )
    parser.add_argument("--unit-junit", default=None, help="Unit tier junit XML.")
    parser.add_argument(
        "--integration-junit", default=None, help="Integration tier junit XML."
    )
    parser.add_argument(
        "--e2e-junit",
        default=None,
        help="E2E tier junit XML. Omit when e2e did not run — the e2e tier is "
        "then marked not-applicable (no grade cap, excluded from the aggregate).",
    )
    parser.add_argument("--unit-coverage", default=None, help="Unit coverage.json.")
    parser.add_argument(
        "--integration-coverage", default=None, help="Integration coverage.json."
    )
    # Deprecated single-file aliases (pre-tier-split); map to the unit tier.
    parser.add_argument(
        "--junit", default=None, help="Deprecated alias for --unit-junit."
    )
    parser.add_argument(
        "--coverage", default=None, help="Deprecated alias for --unit-coverage."
    )
    parser.add_argument(
        "--repo",
        required=True,
        help='GitHub full name, e.g. "atlanhq/atlan-mysql-app".',
    )
    parser.add_argument("--commit", default=None, help="Commit SHA.")
    parser.add_argument(
        "--app", default=None, help="App name (default: derived from --repo)."
    )
    parser.add_argument("--rubric", default="v1", help="Rubric version (default: v1).")
    parser.add_argument(
        "--tool-version",
        default=None,
        help="Scorecard tool version (default: the conformance package version).",
    )
    parser.add_argument(
        "--out",
        default="results/test-readiness.json",
        help="Output path for the scorecard JSON.",
    )
    args = parser.parse_args(argv)

    from conformance import __version__

    unit_junit = args.unit_junit or args.junit
    unit_coverage = args.unit_coverage or args.coverage
    if not unit_junit:
        parser.error("at least --unit-junit (or the deprecated --junit) is required")

    tests = RawTests(
        unit=_counts(unit_junit),
        integration=_counts(args.integration_junit),
        e2e=_counts(args.e2e_junit),
    )

    # unit + integration are always measured; e2e only when its junit is given.
    measured_tiers: set[TierName] = {"unit", "integration"}
    if args.e2e_junit:
        measured_tiers.add("e2e")

    coverage: dict[TierName, CoverageMetrics] = {}
    if (unit_cov := _coverage(unit_coverage)) is not None:
        coverage["unit"] = unit_cov
    if (int_cov := _coverage(args.integration_coverage)) is not None:
        coverage["integration"] = int_cov

    scorecard = build_scorecard(
        tests=tests,
        coverage=coverage,
        measured_tiers=measured_tiers,
        rubric=load_rubric(args.rubric),
        repo=args.repo,
        app=args.app or _app_from_repo(args.repo),
        commit_sha=args.commit,
        tool_version=args.tool_version or __version__,
        generated_at=_now_iso(),
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(scorecard.model_dump(by_alias=True, exclude_none=True), indent=2)
        + "\n",
        encoding="utf-8",
    )

    agg = scorecard.aggregate
    capped = f" (capped by {', '.join(agg.capped_by)})" if agg.capped_by else ""
    print(
        f"{args.repo}: score={agg.score} grade={agg.grade} "
        f"maturity={agg.maturity}{capped} → {out_path}"
    )
    return 0
