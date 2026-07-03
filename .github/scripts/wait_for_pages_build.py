#!/usr/bin/env python3
"""Wait for a GitHub Pages build of the current commit, triggering one if needed.

Invoked by .github/workflows/contract-toolkit-publish.yml after the built
contract-toolkit package is pushed to the gh-pages branch. Moved out of
inlined shell per docs/standards/ci.md ("No conditional logic in inlined
shell"): the skip-if-already-built check and the poll loop both branch on
state, so they belong in a tested driver, not a workflow `run:` block.

Exits 0 once the target commit's Pages build reaches "built" — including
immediately, without triggering a redundant new build, if it is already
built. Exits 1 if the build errors, or once the poll budget is exhausted.

Each poll reads `status` and `commit` from a single `gh api
.../pages/builds/latest` response rather than two separate calls: the
"latest" build can change between two independent requests, which would let
a caller read `status` from one build and `commit` from another.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a subprocess and capture output. Single seam so tests can stub gh/git."""
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def current_commit() -> str:
    return run(["git", "rev-parse", "HEAD"]).stdout.strip()


def latest_build(repo: str) -> dict:
    result = run(["gh", "api", f"repos/{repo}/pages/builds/latest"])
    return json.loads(result.stdout)


def trigger_build(repo: str) -> None:
    run(["gh", "api", "--method", "POST", f"repos/{repo}/pages/builds"])


def wait_for_build(
    repo: str,
    pages_sha: str,
    *,
    max_attempts: int = 300,
    sleep_seconds: int = 5,
    sleep=time.sleep,
) -> bool:
    """Poll until `pages_sha`'s Pages build is `built`.

    Returns True on success (already built, or reached `built` within the poll
    budget), False on an `errored` build or timeout.
    """
    build = latest_build(repo)
    if build.get("commit") == pages_sha and build.get("status") == "built":
        print(f"Pages already built for {pages_sha}; skipping rebuild trigger.")
        return True

    trigger_build(repo)

    for _ in range(max_attempts):
        build = latest_build(repo)
        if build.get("commit") != pages_sha:
            sleep(sleep_seconds)
            continue

        status = build.get("status")
        if status == "built":
            return True
        if status == "errored":
            message = (build.get("error") or {}).get("message") or "unknown error"
            print(f"::error::GitHub Pages build failed: {message}")
            return False

        sleep(sleep_seconds)

    print(f"::error::Timed out waiting for GitHub Pages build for {pages_sha}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/application-sdk"
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=300,
        help="Poll iterations before giving up (default: 300 * 5s = 25 minutes).",
    )
    parser.add_argument("--sleep-seconds", type=int, default=5)
    args = parser.parse_args(argv)

    pages_sha = current_commit()
    ok = wait_for_build(
        args.repo,
        pages_sha,
        max_attempts=args.max_attempts,
        sleep_seconds=args.sleep_seconds,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
