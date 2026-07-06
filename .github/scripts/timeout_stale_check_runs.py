#!/usr/bin/env python3
"""Watchdog for the cross-repo e2e callback pattern: time out any
``Connector E2E / *`` check run that's been stuck ``in_progress`` past the
window a connector run could legitimately still be running.

This only matters when a connector runner dies before its ``report-to-sdk``
callback step (complete_check_run.py) gets to run (crash, force-cancel,
runner reclaim) — the check run would otherwise sit "in_progress" forever.
It is *not* what makes a merge queue entry fail promptly: the gate job
(poll_check_runs_gate.py) is bounded by its own timeout regardless. This is
cosmetic-but-important cleanup so an abandoned PR doesn't show a permanently
pending check.

Sweeps open PRs only — a merge-queue gate resolves (and times out) within its
own run, so there's nothing there to leak.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI."""
    return subprocess.run(cmd, **kwargs)


def list_open_prs(repo: str) -> list[dict]:
    result = run(
        ["gh", "api", f"repos/{repo}/pulls?state=open&per_page=100"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to list open PRs for {repo}: {result.stderr}"
        )
    return json.loads(result.stdout)


def list_check_runs(repo: str, sha: str) -> list[dict]:
    result = run(
        ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to list check runs for {repo}@{sha}: {result.stderr}"
        )
    return json.loads(result.stdout).get("check_runs", [])


def is_stale(check_run: dict, max_age: timedelta, now: datetime) -> bool:
    if check_run.get("status") != "in_progress":
        return False
    started = check_run.get("started_at")
    if not started:
        return False
    started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return (now - started_dt) > max_age


def timeout_check_run(repo: str, check_run_id: int, name: str) -> None:
    payload = {
        "status": "completed",
        "completed_at": now_iso(),
        "conclusion": "timed_out",
        "output": {
            "title": name,
            "summary": (
                "Timed out by the e2e-callback watchdog: no completion signal "
                "arrived within the expected window. The connector run likely "
                "crashed or was cancelled before it could report back."
            ),
        },
    }
    result = run(
        [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{repo}/check-runs/{check_run_id}",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to time out check run {check_run_id} on {repo}: {result.stderr}"
        )


def sweep(
    repo: str,
    name_prefix: str,
    max_age_minutes: int,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Time out stale check runs across every open PR. Returns the list of
    'name (PR #n)' descriptions that were timed out, for logging."""
    now = now or datetime.now(timezone.utc)
    max_age = timedelta(minutes=max_age_minutes)
    timed_out = []

    for pr in list_open_prs(repo):
        sha = pr["head"]["sha"]
        for check_run in list_check_runs(repo, sha):
            name = check_run.get("name", "")
            if not name.startswith(name_prefix):
                continue
            if is_stale(check_run, max_age, now):
                timeout_check_run(repo, check_run["id"], name)
                timed_out.append(f"{name} (PR #{pr['number']})")

    return timed_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/application-sdk"
    )
    parser.add_argument("--name-prefix", default="Connector E2E / ")
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=130,
        help="Age past which an in_progress check is considered stuck (default 130min, "
        "safely above the 120min connector job ceiling).",
    )
    args = parser.parse_args(argv)

    timed_out = sweep(args.repo, args.name_prefix, args.max_age_minutes)
    if timed_out:
        print(f"Timed out {len(timed_out)} stuck check run(s): {timed_out}")
    else:
        print("No stuck check runs found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
