"""``scorecard`` subcommand — emit a test-readiness scorecard JSON.

Reads the standard test-evidence artifacts a CI run already produces (pytest
junit XML + coverage.py JSON), scores them against the bundled rubric, and
writes ``results/test-readiness.json``.

This is the one impure edge of the scorecard pipeline: it touches the
filesystem and stamps ``generatedAt``.  All scoring logic lives in the pure
``readers`` + ``compute`` modules.

Usage::

    atlan-application-sdk-conformance scorecard \\
        --junit results/test-results.xml \\
        --coverage coverage.json \\
        --repo "$GITHUB_REPOSITORY" \\
        --commit "$GITHUB_SHA" \\
        --out results/test-readiness.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path

from conformance.scorecard.compute import build_scorecard
from conformance.scorecard.readers import parse_coverage_json, parse_junit
from conformance.scorecard.rubric import load_rubric
from conformance.scorecard.schema import RawTests


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _app_from_repo(repo: str) -> str:
    """Derive a short app name from ``owner/atlan-<app>-app`` (best effort)."""
    name = repo.split("/")[-1]
    if name.startswith("atlan-") and name.endswith("-app"):
        return name[len("atlan-") : -len("-app")]
    return name


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="atlan-application-sdk-conformance scorecard",
        description="Emit a test-readiness scorecard from junit + coverage evidence.",
    )
    parser.add_argument(
        "--junit",
        required=True,
        help="Path to the pytest junit XML (e.g. results/test-results.xml).",
    )
    parser.add_argument(
        "--coverage",
        default=None,
        help="Path to coverage.py JSON (coverage.json). Omit if unavailable.",
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

    junit_path = Path(args.junit)
    if not junit_path.exists():
        parser.error(f"junit file not found: {junit_path}")

    tests: RawTests = parse_junit(junit_path)

    coverage = None
    if args.coverage:
        cov_path = Path(args.coverage)
        if cov_path.exists():
            coverage = parse_coverage_json(cov_path)
        else:
            print(f"warning: coverage file not found, scoring without it: {cov_path}")

    scorecard = build_scorecard(
        tests=tests,
        coverage=coverage,
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
