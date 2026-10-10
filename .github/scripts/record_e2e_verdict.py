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

The check before the write races a newer attempt's ``pending``, so the recorder
re-checks after writing (``reconcile``).

Fail-open, always exit 0, like the step it replaces. If the attempts cannot be
read it records anyway: a lingering ``pending`` from this run is worse than a
rare stale row.

Unit-tested in tests/test_record_e2e_verdict.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_commit_verdict import (  # noqa: E402
    E2E_CONTEXT,
    RunFn,
    SleepFn,
    StatusUnreadable,
    _run_gh,
    attempt_id,
    read_e2e_attempts,
    read_e2e_rows,
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
    sleep: SleepFn = time.sleep,
) -> bool:
    """Post the verdict. Returns False when skipped or rejected."""
    run = run or _run_gh
    # This check alone races: a newer attempt can post `pending` after it and
    # before the write below. reconcile() re-checks after the write and
    # re-posts that attempt's row, so the combined status cannot show ours.
    if _newer_attempt_owns_verdict(repo, sha, run_id, state, run):
        return False
    if not _post(run, repo, sha, state, description, target_url):
        print(
            f"::warning::could not record the e2e verdict on {sha[:7]}", file=sys.stderr
        )
        return False
    reconcile(repo, sha, run, sleep=sleep)
    return True


def _newer_attempt_owns_verdict(
    repo: str, sha: str, run_id: int, state: str, run: RunFn
) -> bool:
    try:
        attempts = read_e2e_attempts(repo, sha, run)
    except StatusUnreadable as exc:
        print(f"::warning::{exc}; recording the verdict anyway", file=sys.stderr)
        return False
    newer = [attempt for attempt in attempts if attempt > run_id]
    if newer:
        print(
            f"::notice::run {max(newer)} started a newer e2e attempt on {sha[:7]}; "
            f"it owns the verdict, so this run's `{state}` is not recorded",
            file=sys.stderr,
        )
    return bool(newer)


#: How many times to re-check that the combined status shows the newest attempt.
_RECONCILE_PASSES = 3

#: The pause before each re-check, long enough for a racing write to land.
_RECONCILE_PAUSE_SECONDS = 5


def _post(
    run: RunFn, repo: str, sha: str, state: str, description: str, target_url: str
) -> bool:
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
    return bool(posted.strip())


def reconcile(
    repo: str,
    sha: str,
    run: RunFn,
    *,
    sleep: SleepFn = time.sleep,
    passes: int = _RECONCILE_PASSES,
    pause: float = _RECONCILE_PAUSE_SECONDS,
) -> bool:
    """Make the combined ``e2e`` status show the newest attempt's latest row.

    The check before the write is not enough on its own. A newer attempt can
    post ``pending`` between this run's read and its write, and this run's late
    row then becomes the one the combined status shows. So after writing, the
    recorder re-reads the history and, if the newest row does not belong to the
    newest attempt, re-posts that attempt's latest row (state, description,
    link) so the combined status matches what the Tests Gate reads. Every
    recorder does this, including the newer attempt's own, so the last writer
    always restores the newest attempt. A re-post that lands after the newer
    attempt's final verdict re-posts its ``pending``: that residual race blocks
    the commit rather than passing it.

    Returns True once it does, False if it could not confirm it.
    """
    for _ in range(passes):
        sleep(pause)
        try:
            rows = read_e2e_rows(repo, sha, run)
        except StatusUnreadable as exc:
            print(f"::warning::{exc}; cannot confirm the e2e status", file=sys.stderr)
            return False
        if not rows:
            return True
        newest_attempt = max(attempt_id(row) for row in rows)
        if attempt_id(rows[0]) == newest_attempt:
            return True
        latest = next(row for row in rows if attempt_id(row) == newest_attempt)
        print(
            f"::notice::an older e2e attempt's row masks run {newest_attempt} on "
            f"{sha[:7]}; re-posting that attempt's `{latest['state']}`",
            file=sys.stderr,
        )
        _post(
            run,
            repo,
            sha,
            latest["state"],
            str(latest.get("description") or ""),
            str(latest.get("target_url") or ""),
        )
    print(
        f"::warning::could not confirm the e2e status on {sha[:7]} shows the newest "
        "attempt",
        file=sys.stderr,
    )
    return False


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
