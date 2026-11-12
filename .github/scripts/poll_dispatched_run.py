#!/usr/bin/env python3
"""Poll a cross-repo dispatched workflow run to completion (e2e-apps poll mode).

Replaces the inlined ``while``/``if`` shell loop that used to live in
``.github/actions/e2e-apps/action.yaml``. Branching logic in a workflow cannot
be regression-tested (docs/standards/ci.md), and this loop had accumulated two
behaviours worth pinning down with tests:

1. **A transient API error must not look like a finished run.** The shell did
   ``status=$(echo $response | jq -r '.status')`` with no error handling, so a
   single 5xx / timeout / rate-limit blip anywhere in a two-hour poll set
   ``status`` to ``null``, fell straight out of the ``while`` (``null`` is not
   ``in_progress``), and reported ``conclusion=null`` — failing the SDK job
   while the dispatched run was still happily running. On the merge-queue path
   that ejects the PR. Here a failed read is retried, and only
   ``--max-consecutive-errors`` reads in a row give up.

2. **Timeout is not failure.** Both then and now the timeout path exits 0 and
   reports ``conclusion=timeout``; the caller's own "Fail job if dispatched run
   did not succeed" step is the single place that turns a non-success
   conclusion into a red job.

The heartbeat line shape is deliberately preserved::

    🔄 dispatched run [ 60s] in_progress

so an SDK-side log still scans the same way as the connector-side worker log.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Sibling .github/scripts module — imported, not copied (FND-2674, FND-4899).
# Run as `python3 <path>/poll_dispatched_run.py` the script's own dir is on
# sys.path already; the insert covers importers loading it by file path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gh_refs import gh_request  # noqa: E402

# Statuses that mean "not finished yet"; anything else ends the poll.
PENDING_STATUSES = {"in_progress", "queued", "pending", "waiting", "requested"}

GLYPHS = {
    "queued": "🟡",
    "waiting": "🟡",
    "pending": "🟡",
    "requested": "🟡",
    "in_progress": "🔄",
    "completed": "✅",
}


def glyph_for_status(status: str) -> str:
    return GLYPHS.get(status, "❔")


def fetch_run(repo: str, run_id: str) -> dict:
    """Return the run object, or raise RuntimeError on any read failure.

    Raising rather than returning a sentinel is the point: the caller decides
    that a failed read means "poll again", which is the behaviour the old shell
    got wrong by conflating an unreadable response with a concluded run.

    The request goes through ``_gh_refs.gh_request``, the shared curl transport,
    which feeds the token to curl on stdin rather than argv (FND-4899): this poll
    holds the fleet App installation token for up to two hours, and an argv
    token is readable by anything else on the runner for every one of those
    requests. The transport also retries a transient curl failure or 5xx before
    this function sees it, beneath ``poll``'s own consecutive-error budget.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("::error::GH_TOKEN (or GITHUB_TOKEN) must be set")

    try:
        response = gh_request("GET", f"repos/{repo}/actions/runs/{run_id}")
    except SystemExit as exc:
        # The transport exits once its own retries are spent, or on a response it
        # cannot parse at all. Either is one unreadable poll, not a verdict.
        raise RuntimeError(str(exc)) from None
    if response.status >= 400:
        raise RuntimeError(
            f"HTTP {response.status}: {response.message or 'no message'}"
        )
    payload = response.body
    if not isinstance(payload, dict) or "status" not in payload:
        raise RuntimeError(
            f"HTTP {response.status} response has no status field: {payload!r:.200}"
        )
    return payload


def poll(
    repo: str,
    run_id: str,
    *,
    interval_seconds: int = 60,
    timeout_seconds: int = 7200,
    max_consecutive_errors: int = 5,
    sleep=None,
    monotonic=None,
) -> tuple[str, str]:
    """Poll until the run leaves a pending status, or the budget elapses.

    Returns ``(status, conclusion)``. On timeout both are ``"timeout"``.

    ``sleep``/``monotonic`` default to ``None`` and are resolved from ``time``
    here rather than being bound as default arguments: a default argument is
    evaluated once at import, so it captures the original function and a test
    that patches ``time.sleep`` would still sleep for real (this cost the
    main() test a literal 60 seconds before it was written this way).
    """
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    started = monotonic()
    consecutive_errors = 0

    while True:
        sleep(interval_seconds)

        try:
            payload = fetch_run(repo, run_id)
        except RuntimeError as exc:
            consecutive_errors += 1
            # Deliberately a warning, not an error: one unreadable poll says
            # nothing about the dispatched run, and treating it as terminal is
            # the exact bug this replaces.
            print(
                f"::warning::could not read run {run_id} in {repo} "
                f"({consecutive_errors}/{max_consecutive_errors} consecutive): {exc}"
            )
            if consecutive_errors >= max_consecutive_errors:
                print(
                    f"::error::giving up after {consecutive_errors} consecutive "
                    f"failed reads of run {run_id} in {repo}"
                )
                return "unreadable", "unreadable"
            if int(monotonic() - started) >= timeout_seconds:
                print(
                    f"⏰ Workflow run timeout reached after {timeout_seconds} seconds"
                )
                return "timeout", "timeout"
            continue

        consecutive_errors = 0
        status = str(payload.get("status") or "")
        conclusion = str(payload.get("conclusion") or "")
        elapsed = int(monotonic() - started)
        print(f"{glyph_for_status(status)} dispatched run [{elapsed:4d}s] {status}")

        if status not in PENDING_STATUSES:
            return status, conclusion

        if elapsed >= timeout_seconds:
            print(f"⏰ Workflow run timeout reached after {timeout_seconds} seconds")
            return "timeout", "timeout"


def write_outputs(status: str, conclusion: str) -> None:
    """Append status/conclusion to $GITHUB_OUTPUT when running under Actions."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"status={status}\n")
        handle.write(f"conclusion={conclusion}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo of the DISPATCHED run's repository."
    )
    parser.add_argument("--run-id", required=True, help="The dispatched run's id.")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        help="Heartbeat cadence. 60s matches the connector-side AE poll cadence so "
        "the SDK-side log doesn't out-spam the dispatched run's own progress lines.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=7200,
        help="Overall poll budget (default 7200s = 120min, matching the dispatched "
        "workflow's own timeout-minutes ceiling).",
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=5,
        help="Consecutive unreadable polls tolerated before giving up.",
    )
    args = parser.parse_args(argv)

    print(f"Checking status of workflow run {args.run_id} in {args.repo}")
    status, conclusion = poll(
        args.repo,
        args.run_id,
        interval_seconds=args.interval_seconds,
        timeout_seconds=args.timeout_seconds,
        max_consecutive_errors=args.max_consecutive_errors,
    )
    write_outputs(status, conclusion)

    if conclusion != "success":
        print(
            f"Workflow run {args.run_id} in {args.repo} failed with conclusion {conclusion}"
        )
    else:
        print(f"Workflow run {args.run_id} in {args.repo} succeeded")
    # Always 0: the caller's dedicated "Fail job if dispatched run did not
    # succeed" step owns turning a conclusion into an exit code, so that the
    # PR-comment steps in between still run.
    return 0


if __name__ == "__main__":
    sys.exit(main())
