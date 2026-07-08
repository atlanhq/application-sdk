#!/usr/bin/env python3
"""Patch an existing check run's ``details_url`` without touching its status.

Used by e2e-apps' callback mode: the check is created immediately (so it's
visible right away) with a generic fallback link to the connector's Actions
tab, since the dispatched run's own URL isn't known yet. Once
codex-/return-dispatch locates that run (a small, bounded lookup — not the
old multi-hour status-wait this callback pattern replaced), this patches in
the precise run link so the pending check is actually clickable while it's
still in progress, not just after it completes.

Best-effort by design: the caller should treat a failure here as
non-fatal (`continue-on-error: true`) — the check still works correctly
without ever getting the precise link, it's just less convenient to click
through from.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI."""
    return subprocess.run(cmd, **kwargs)


def update_details_url(repo: str, check_run_id: int, details_url: str) -> None:
    payload = {"details_url": details_url}
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
            f"::error::failed to update details_url on check run {check_run_id} "
            f"on {repo}: {result.stderr}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/application-sdk"
    )
    parser.add_argument("--check-run-id", required=True, type=int)
    parser.add_argument("--details-url", required=True)
    args = parser.parse_args(argv)

    update_details_url(args.repo, args.check_run_id, args.details_url)
    print(f"Updated details_url on check run {args.check_run_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
