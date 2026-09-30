#!/usr/bin/env python3
"""Fail a Renovate PR's Tests Gate unless ``renovate/artifacts`` is ``success``.

Why this exists
---------------
A failed ``postUpgradeTasks`` command does not stop Renovate: it still raises the
PR and still enables platform automerge (see
``renovate_approval_conditions.classify_artifact_state``). The approval gate
withholds on it, but a repo whose ``main`` ruleset requires no approval merges
on required checks alone, and ``renovate/artifacts`` is not one of them. Since
the SDK, conformance and contract-toolkit bumps share one grouped PR (FND-2868),
a failed ``renovate-pkl-sync`` or ``renovate-contract-ledger`` would land
together with the SDK bump. Tests Gate is required fleet-wide, so failing it
here is what makes a failed member task hold the group.

Behaviour
---------
* Head ref not under ``renovate/``: pass at once. This job runs on every PR,
  push and merge-group run in every app repo, so this path must never wait or
  call the API.
* Renovate PR: read ``renovate/artifacts`` on the head SHA. Renovate pushes the
  branch before it posts statuses, so an absent or ``pending`` context is
  polled for up to a minute. Renovate posts it seconds after the push, so the
  poll only runs out when the status is never coming. ``success`` passes;
  anything else fails, including still-absent once the poll is spent. Failing
  on absent is only safe because the fleet preset sets
  ``statusCheckWhen.artifactError = "always"``.
* Renovate stamps ``renovate/artifacts`` only on commits it authored, so a
  commit pushed to a ``renovate/*`` branch by a human or another bot leaves the
  head unstamped and this gate red; recover by having Renovate rebase the PR.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

from renovate_approval_conditions import (
    ARTIFACT_CONTEXT,
    ARTIFACT_MISSING,
    classify_artifact_state,
)

RENOVATE_PREFIX = "renovate/"
SUCCESS = "success"
WAIT_STATES = frozenset({ARTIFACT_MISSING, "pending"})

POLL_ATTEMPTS = 6
POLL_INTERVAL_SECONDS = 10
API_TIMEOUT_SECONDS = 20

sleep = time.sleep


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    # A stalled call is a failed fetch, not a hung job: without this bound one
    # stuck request would hold the required check until the job timeout.
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=API_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            command, 1, "", f"timed out after {API_TIMEOUT_SECONDS}s"
        )


def fetch_status(repo: str, sha: str) -> Any:
    """Return the combined status with every page's ``statuses`` merged.

    The endpoint pages its ``statuses`` list, so a SHA with many contexts can
    carry ``renovate/artifacts`` past the first page; reading only that page
    would fail a healthy PR as missing.
    """
    result = run(
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{sha}/status?per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    if result.returncode != 0:
        print(
            f"Could not read statuses for {sha[:7]}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Status payload for {sha[:7]} was not JSON.", file=sys.stderr)
        return None
    if not isinstance(pages, list):
        return None
    statuses = [
        entry
        for page in pages
        if isinstance(page, dict) and isinstance(page.get("statuses"), list)
        for entry in page["statuses"]
    ]
    return {"statuses": statuses}


def await_artifact_state(repo: str, sha: str, attempts: int, interval: float) -> str:
    state = ARTIFACT_MISSING
    for attempt in range(1, attempts + 1):
        state = classify_artifact_state(fetch_status(repo, sha))
        if state not in WAIT_STATES:
            return state
        if attempt < attempts:
            print(
                f"{ARTIFACT_CONTEXT} is {state} on {sha[:7]} (attempt {attempt}/{attempts}); waiting."
            )
            sleep(interval)
    return state


def evaluate(head_ref: str, repo: str, sha: str, attempts: int, interval: float) -> int:
    if not head_ref.startswith(RENOVATE_PREFIX):
        print(f"Not a Renovate branch ({head_ref or 'no head ref'}); nothing to check.")
        return 0
    if not repo or not sha:
        print("A Renovate branch needs --repo and --sha.", file=sys.stderr)
        return 1
    state = await_artifact_state(repo, sha, attempts, interval)
    if state == SUCCESS:
        print(f"{ARTIFACT_CONTEXT} is {SUCCESS} on {sha[:7]}.")
        return 0
    print(
        f"::error::{ARTIFACT_CONTEXT} is {state} on {sha[:7]}: a Renovate post-upgrade task "
        "failed or did not run, so this PR must not merge until it is green."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--sha", default="")
    parser.add_argument("--poll-attempts", type=int, default=POLL_ATTEMPTS)
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_SECONDS)
    args = parser.parse_args(argv)
    return evaluate(
        args.head_ref, args.repo, args.sha, args.poll_attempts, args.poll_interval
    )


if __name__ == "__main__":
    sys.exit(main())
