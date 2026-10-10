#!/usr/bin/env python3
"""Record this run's e2e verdict on the PR head, unless a newer attempt owns it.

The run that acted on the ``e2e`` label replaces its ``pending`` status with
``success``, ``failure`` or ``error`` (FND-3650). Two attempts can share a
commit: the label added twice, or removed and re-added while a run is still
going. The Tests Gate reads every attempt and lets the newest decide
(``e2e_commit_verdict.py``), but the Release Gate reads the combined status,
which shows only the newest ``e2e`` row. So an older attempt finishing after a
newer one started must not write: its row would mask the newer attempt's
``pending`` or verdict there.

Fail-open, always exit 0, like the step it replaces. If the attempts cannot be
read it records anyway: a lingering ``pending`` from this run is worse than a
rare stale row.

Unit-tested in tests/test_record_e2e_verdict.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_commit_verdict import (  # noqa: E402
    E2E_CONTEXT,
    RunFn,
    StatusUnreadable,
    _run_gh,
    read_e2e_attempts,
)


def record(
    repo: str,
    sha: str,
    run_id: int,
    state: str,
    description: str,
    target_url: str,
    *,
    run: RunFn | None = None,
) -> bool:
    """Post the verdict. Returns False when skipped or rejected."""
    run = run or _run_gh
    try:
        attempts = read_e2e_attempts(repo, sha, run)
    except StatusUnreadable as exc:
        print(f"::warning::{exc}; recording the verdict anyway", file=sys.stderr)
        attempts = {}
    newer = [attempt for attempt in attempts if attempt > run_id]
    if newer:
        print(
            f"::notice::run {max(newer)} started a newer e2e attempt on {sha[:7]}; "
            f"it owns the verdict, so this run's `{state}` is not recorded",
            file=sys.stderr,
        )
        return False
    posted = run(
        [
            "api",
            "-X",
            "POST",
            f"repos/{repo}/statuses/{sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={E2E_CONTEXT}",
            "-f",
            f"description={description}",
            "-f",
            f"target_url={target_url}",
        ]
    )
    if not posted.strip():
        print(
            f"::warning::could not record the e2e verdict on {sha[:7]}", file=sys.stderr
        )
        return False
    return True


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record this run's e2e verdict.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument(
        "--state", required=True, choices=["success", "failure", "error"]
    )
    parser.add_argument("--description", required=True)
    parser.add_argument("--target-url", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    record(
        args.repo,
        args.head_sha,
        args.run_id,
        args.state,
        args.description,
        args.target_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
