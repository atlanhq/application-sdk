#!/usr/bin/env python3
"""Complete a GitHub check run by name — the callback side of the cross-repo
e2e callback pattern.

Called from the *connector* repo's own workflow run (``tests-reusable.yaml``'s
``report-to-sdk`` job), authenticated with the org PAT, at the very end of the
run (``if: always()``). It finds the check run that ``create_check_run.py``
created on application-sdk's PR SHA and PATCHes it to ``completed`` with the
real conclusion and the rendered report as output. This is the push: the
SDK-side check flips the instant this script runs, with no polling on either
side.

Falls back to creating the check run directly (as already-completed) if none
is found — covers a manual/legacy dispatch that skipped the create step.

Summary text is read from --summary-file when given (the connector's already-
rendered pr-comment-body.md), else --summary. Truncated defensively: the
Checks API output.summary field has a 65535-character limit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

MAX_SUMMARY_CHARS = 60_000


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI."""
    return subprocess.run(cmd, **kwargs)


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


def find_check_run(check_runs: list[dict], name: str) -> dict | None:
    matches = [c for c in check_runs if c.get("name") == name]
    if not matches:
        return None
    # Most recently created wins, in case a retry left more than one.
    return max(matches, key=lambda c: c.get("id", 0))


def truncate_summary(summary: str) -> str:
    if len(summary) <= MAX_SUMMARY_CHARS:
        return summary
    return summary[:MAX_SUMMARY_CHARS] + "\n\n… (truncated)"


def complete_check_run(
    repo: str,
    sha: str,
    name: str,
    conclusion: str,
    summary: str,
    *,
    title: str | None = None,
    details_url: str | None = None,
) -> None:
    existing = find_check_run(list_check_runs(repo, sha), name)
    payload = {
        "name": name,
        "head_sha": sha,
        "status": "completed",
        "completed_at": now_iso(),
        "conclusion": conclusion,
        "output": {"title": title or name, "summary": truncate_summary(summary)},
    }
    if details_url:
        payload["details_url"] = details_url

    if existing:
        cmd = [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{repo}/check-runs/{existing['id']}",
            "--input",
            "-",
        ]
        target = f"check run id={existing['id']}"
    else:
        cmd = [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo}/check-runs",
            "--input",
            "-",
        ]
        target = "new check run (no matching in-progress run found)"

    result = run(
        cmd, input=json.dumps(payload), capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to complete {target} on {repo}@{sha}: {result.stderr}"
        )
    print(f"Completed '{name}' on {repo}@{sha} ({target}) — conclusion={conclusion}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        required=True,
        help="owner/repo the check run lives on, e.g. atlanhq/application-sdk",
    )
    parser.add_argument(
        "--sha", required=True, help="Head SHA the check run is attached to."
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Check run name, e.g. 'Connector E2E / atlan-mysql-app'.",
    )
    parser.add_argument(
        "--conclusion",
        required=True,
        choices=[
            "success",
            "failure",
            "cancelled",
            "timed_out",
            "neutral",
            "action_required",
            "skipped",
        ],
    )
    parser.add_argument("--title", default=None)
    parser.add_argument("--details-url", default=None)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--summary", help="Inline summary text.")
    group.add_argument(
        "--summary-file", help="Path to a file containing the summary markdown."
    )
    args = parser.parse_args(argv)

    if args.summary_file:
        try:
            with open(args.summary_file, encoding="utf-8") as fh:
                summary = fh.read()
        except OSError as e:
            raise SystemExit(
                f"::error::could not read --summary-file {args.summary_file}: {e}"
            )
    else:
        summary = args.summary

    complete_check_run(
        args.repo,
        args.sha,
        args.name,
        args.conclusion,
        summary,
        title=args.title,
        details_url=args.details_url,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
