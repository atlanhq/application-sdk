#!/usr/bin/env python3
"""Flag jobs in this workflow run that were killed by their ``timeout-minutes``.

A job that hits its ``timeout-minutes`` cap concludes ``cancelled`` — the same
conclusion as a job cancelled because a newer push superseded it. In run
tallies the two are indistinguishable, so a timeout-shaped flake class reads as
CI noise until someone opens the logs.

This reads every job of the current run attempt from the API, works out each
job's cap from the workflow YAML (following local reusable-workflow calls, so
``SDK Tests / Unit Tests (3.11, ubuntu-22.04)`` resolves to the ``unit`` job in
sdk-tests-reusable.yaml), and emits a ``::warning::`` plus a step-summary line
for any job whose duration came within ``--margin-seconds`` of that cap.

Advisory only: it always exits 0 once it has a job list, and the gate step runs
with continue-on-error, so it can never be the reason a gate fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

# GitHub's default when a job sets no timeout-minutes.
DEFAULT_TIMEOUT_MINUTES = 360
API_TIMEOUT_SECONDS = 120
# Conclusions a timeout kill can produce; a near-cap `success` only ran close.
_KILLED = ("cancelled", "failure", "timed_out")

_EXPR = re.compile(r"\$\{\{.*?\}\}")


@dataclass(frozen=True)
class JobCap:
    pattern: re.Pattern[str]
    display: str
    minutes: int


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the ``gh`` CLI."""
    return subprocess.run(cmd, **kwargs)


def _name_pattern(display: str) -> re.Pattern[str]:
    """Regex for a job's API name: expressions match anything, and a matrix
    job's ``(v1, v2)`` suffix is optional."""
    parts = _EXPR.split(display)
    body = ".+?".join(re.escape(p) for p in parts)
    return re.compile(rf"{body}(?: \(.*\))?")


def _cap_minutes(job: dict) -> int | None:
    raw = job.get("timeout-minutes", DEFAULT_TIMEOUT_MINUTES)
    if isinstance(raw, int):
        return raw
    # An expression (or anything else non-literal) has no knowable cap here.
    return None


def collect_caps(
    workflow: Path, repo_root: Path, prefix: str = "", depth: int = 0
) -> list[JobCap]:
    """Every job in ``workflow`` (recursing into local reusables) with its cap."""
    if depth > 4:
        return []
    doc = yaml.safe_load(workflow.read_text()) or {}
    caps: list[JobCap] = []
    for key, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        name = str(job.get("name", key))
        display = f"{prefix}{name}"
        uses = job.get("uses")
        if isinstance(uses, str):
            if uses.startswith("./"):
                caps.extend(
                    collect_caps(
                        repo_root / uses[2:], repo_root, f"{display} / ", depth + 1
                    )
                )
            continue
        minutes = _cap_minutes(job)
        if minutes is not None:
            caps.append(JobCap(_name_pattern(display), display, minutes))
    return caps


def list_jobs(repo: str, run_id: str, attempt: str) -> list[dict]:
    cmd = [
        "gh",
        "api",
        "--paginate",
        f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
        "--jq",
        ".jobs[] | tojson",
    ]
    try:
        result = run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            # A stalled request must not hold the gate until its own timeout.
            timeout=API_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"::warning::listing jobs for run {run_id} timed out after "
            f"{API_TIMEOUT_SECONDS}s; no timeout report for this run"
        ) from None
    if result.returncode != 0:
        raise SystemExit(
            f"::warning::failed to list jobs for run {run_id}: {result.stderr}"
        )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def find_timeouts(
    jobs: list[dict], caps: list[JobCap], margin_seconds: int
) -> list[tuple[str, float, int, str]]:
    """``(job name, duration s, cap min, conclusion)`` for each job that ran to
    within ``margin_seconds`` of its cap."""
    flagged = []
    for job in jobs:
        if job.get("status") != "completed":
            continue
        started, completed = job.get("started_at"), job.get("completed_at")
        if not started or not completed:
            continue
        name = job.get("name", "")
        cap = next((c for c in caps if c.pattern.fullmatch(name)), None)
        if cap is None:
            continue
        duration = (_parse(completed) - _parse(started)).total_seconds()
        if duration >= cap.minutes * 60 - margin_seconds:
            flagged.append((name, duration, cap.minutes, job.get("conclusion") or ""))
    return flagged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument(
        "--workflow",
        required=True,
        type=Path,
        help="path of the workflow file this run executed",
    )
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--margin-seconds", type=int, default=30)
    args = parser.parse_args(argv)

    caps = collect_caps(args.workflow, args.repo_root)
    jobs = list_jobs(args.repo, args.run_id, args.run_attempt)
    flagged = find_timeouts(jobs, caps, args.margin_seconds)

    lines = []
    for name, duration, minutes, conclusion in flagged:
        near = (
            f"{name} ran {duration:.0f}s, within {args.margin_seconds}s of its "
            f"{minutes}-minute timeout-minutes cap (concluded {conclusion})"
        )
        if conclusion in _KILLED:
            title = "Job timeout"
            msg = f"{near}: treat this as a job timeout, not a superseded-push cancellation."
        else:
            title = "Job near its timeout"
            msg = f"{near}: it finished, but the next slow runner will time out."
        print(f"::warning title={title}::{msg}")
        lines.append(f"- {msg}")
    if not flagged:
        print(f"No job ran within {args.margin_seconds}s of its timeout cap.")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and lines:
        with open(summary, "a") as fh:
            fh.write("### Jobs that hit their timeout\n\n" + "\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
