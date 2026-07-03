#!/usr/bin/env python3
"""Wait for a contract-toolkit package to actually be served live on Pages.

Invoked by .github/workflows/contract-toolkit-publish.yml after the built
contract-toolkit package is pushed to the gh-pages branch. Moved out of
inlined shell per docs/standards/ci.md ("No conditional logic in inlined
shell"): the skip-if-already-live check and the poll loop both branch on
state, so they belong in a tested driver, not a workflow `run:` block.

Polls the actual public URL rather than GitHub's internal `gh api
.../pages/builds/*` bookkeeping. That API proved unreliable in practice
(2026-07-03, contract-toolkit v0.17.0 publish, coinciding with a GitHub Pages
incident the day before with the same "slow and failing deployments"
symptom): builds got stuck in `building` for 10+ minutes with zero progress,
or resolved to `errored` for content that was, in fact, already being served
correctly from an earlier successful build — because each retry re-triggers
a brand-new build attempt on top of one that may have already succeeded,
and "latest" only ever reflects the most recent attempt, not the best one.
The published artifact's URL is version-qualified and never reused across
releases, so "is this exact URL returning 200" is unambiguous ground truth
for "is this release live" — independent of how many redundant or stuck
build attempts GitHub's queue is juggling internally.

Exits 0 once the URL is live — including immediately, without triggering a
redundant rebuild, if it already is. Exits 1 once the poll budget is
exhausted without the URL going live.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a subprocess and capture output. Single seam so tests can stub curl/gh."""
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def is_live(url: str) -> bool:
    # --max-time bounds a stalled network request to one poll interval's worth
    # of time, so a hung connection can't block the step indefinitely and
    # bypass max_attempts/heartbeat — the exact failure mode this script
    # exists to avoid.
    result = run(
        ["curl", "-s", "--max-time", "15", "-o", "/dev/null", "-w", "%{http_code}", url]
    )
    return result.stdout.strip() == "200"


def trigger_build(repo: str) -> None:
    run(["gh", "api", "--method", "POST", f"repos/{repo}/pages/builds"])


def wait_for_publish(
    repo: str,
    url: str,
    *,
    max_attempts: int = 300,
    sleep_seconds: int = 5,
    heartbeat_every: int = 12,
    sleep=time.sleep,
) -> bool:
    """Poll `url` until it's live (HTTP 200).

    Returns True on success (already live, or went live within the poll
    budget), False on timeout. Prints a heartbeat every `heartbeat_every`
    attempts so a silent multi-minute wait doesn't look stuck in CI logs.
    """
    if is_live(url):
        print(f"{url} already live; skipping rebuild trigger.")
        return True

    trigger_build(repo)

    for attempt in range(1, max_attempts + 1):
        if is_live(url):
            print(f"{url} is live (after {attempt} attempt(s)).")
            return True

        if attempt % heartbeat_every == 0:
            elapsed = attempt * sleep_seconds
            print(f"Still waiting for {url} to go live ({elapsed}s elapsed)...")

        sleep(sleep_seconds)

    print(f"::error::Timed out waiting for {url} to go live")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/application-sdk"
    )
    parser.add_argument(
        "--url", required=True, help="Public URL the published package must serve from."
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=300,
        help="Poll iterations before giving up (default: 300 * 5s = 25 minutes).",
    )
    parser.add_argument("--sleep-seconds", type=int, default=5)
    args = parser.parse_args(argv)

    ok = wait_for_publish(
        args.repo,
        args.url,
        max_attempts=args.max_attempts,
        sleep_seconds=args.sleep_seconds,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
