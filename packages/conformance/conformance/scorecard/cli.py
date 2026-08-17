"""``scorecard`` subcommand — emit a test-readiness scorecard JSON.

Reads the standard test-evidence artifacts a CI run already produces (pytest
junit XML + coverage.py JSON) and scores them against the bundled rubric.

Post tier-split, unit and integration run as separate CI jobs, so evidence
arrives per-tier: a dedicated aggregation job downloads both jobs' artifacts and
invokes this with per-tier flags.  unit + integration are always scored (a
missing junit → an empty, zero-scored tier, so a missing integration suite
still counts against the grade); e2e is scored only when ``--e2e-junit``
resolves to at least one file — otherwise the e2e tier is marked not-applicable.

``--e2e-junit`` is repeatable and each value may be a **glob**, because the e2e
matrix emits one artifact per suite × cloud leg (FND-6).  Resolving the glob
here rather than in the workflow keeps the "how many legs ran" branching out of
YAML, and makes "matched nothing" mean *not applicable* rather than *scored
zero* — absent evidence must never drag an app's grade (FND-33).

This is the one impure edge: it touches the filesystem and stamps
``generatedAt``.  All scoring logic lives in the pure ``readers`` + ``compute``.

Usage::

    atlan-application-sdk-conformance scorecard \\
        --unit-junit unit/results/test-results.xml \\
        --unit-coverage unit/coverage.json \\
        --integration-junit integration/results/test-results.xml \\
        --integration-coverage integration/coverage.json \\
        --e2e-junit 'e2e-evidence/*/results/sdr-test-results.xml' \\
        --cross-cloud-configured aws,azure,gcp \\
        --cross-cloud-observed aws,azure \\
        --repo "$GITHUB_REPOSITORY" --commit "$GITHUB_SHA" \\
        --out results/test-readiness.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path

from conformance.scorecard.compute import build_scorecard
from conformance.scorecard.readers import (
    parse_coverage_json,
    parse_junit_tier,
    parse_junit_tier_merged,
    resolve_junit_paths,
)
from conformance.scorecard.rubric import load_rubric
from conformance.scorecard.schema import (
    CoverageMetrics,
    CrossCloudCoverage,
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


def _clouds(raw: str | None) -> list[str] | None:
    """Split a comma-separated cloud list; ``None`` (flag absent) stays ``None``.

    The distinction is the whole point of the field: ``None`` is omitted from
    the artifact and means "not known", while ``""`` yields ``[]`` and means
    "known, and it is no clouds" — the degraded single-tenant fallback.
    """
    if raw is None:
        return None
    return [c for c in (part.strip() for part in raw.split(",")) if c]


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
        action="append",
        default=None,
        metavar="PATH_OR_GLOB",
        help="E2E tier junit XML; repeatable, and each value may be a glob "
        "(the e2e matrix emits one artifact per suite x cloud leg). Legs are "
        "folded worst-case per test, so a test failing on any cloud does not "
        "pass. Omit — or pass a glob that matches nothing — when e2e did not "
        "run: the e2e tier is then marked not-applicable (no grade cap, "
        "excluded from the aggregate) rather than scored zero.",
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
    parser.add_argument(
        "--cross-cloud-configured",
        default=None,
        metavar="CLOUDS",
        help="Comma-separated clouds this repo's e2e is WIRED for (the "
        "requested fan-out narrowed to what the tenant matrix carries for it). "
        "Descriptive only, never scored. Omit when unknown; an empty value "
        "records 'no cloud dimension', which is a different fact from omitting.",
    )
    parser.add_argument(
        "--cross-cloud-observed",
        default=None,
        metavar="CLOUDS",
        help="Comma-separated clouds an e2e run actually EXERCISED. Pass only "
        "when e2e ran; omitting it records that nothing is known about "
        "cross-CSP coverage for this run, which must not read as zero.",
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

    # One or many per-leg e2e junits, resolved from repeatable globs. Empty
    # means e2e did not run (or uploaded nothing) — NOT that it scored zero.
    e2e_paths = resolve_junit_paths(args.e2e_junit or [])
    if args.e2e_junit and not e2e_paths:
        print(
            "warning: no e2e junit matched "
            f"{', '.join(args.e2e_junit)} — e2e tier marked not-applicable"
        )

    tests = RawTests(
        unit=_counts(unit_junit),
        integration=_counts(args.integration_junit),
        e2e=parse_junit_tier_merged(e2e_paths),
    )

    # unit + integration are always measured; e2e only when evidence resolved.
    measured_tiers: set[TierName] = {"unit", "integration"}
    if e2e_paths:
        measured_tiers.add("e2e")
        print(f"e2e evidence: {len(e2e_paths)} leg junit(s) merged worst-case per test")

    cross_cloud = None
    configured = _clouds(args.cross_cloud_configured)
    observed = _clouds(args.cross_cloud_observed)
    if configured is not None or observed is not None:
        cross_cloud = CrossCloudCoverage(configured=configured, observed=observed)

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
        cross_cloud=cross_cloud,
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
