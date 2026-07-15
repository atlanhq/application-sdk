#!/usr/bin/env python3
"""Create an in_progress GitHub check run — the dispatch side of the
cross-repo e2e callback pattern.

Used by ``.github/actions/e2e-apps`` (``wait-mode: callback``) right after
dispatching a connector's ``tests.yaml``. The dispatching job creates this
check on *this* repo's PR/merge-queue SHA using ``github.token``, then exits
immediately — no polling. The connector reports back later by PATCHing this
same check run to ``completed`` (see ``complete_check_run.py``), which lives
in the connector's own workflow run and therefore updates the check the
instant the connector's tests finish.

Replaces a busy-poll loop that re-authenticated every request against the
shared org PAT (see docs/standards/ci.md and the e2e-apps action comments)
with a single POST here plus a single PATCH from the connector — the actual
wait no longer costs any API calls at all.

Prints ``check_run_id=<id>`` to $GITHUB_OUTPUT (or stdout if unset), mirroring
the convention in sdk_changes_non_md.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI."""
    return subprocess.run(cmd, **kwargs)


def create_check_run(
    repo: str,
    sha: str,
    name: str,
    *,
    title: str | None = None,
    summary: str,
    details_url: str | None = None,
) -> int:
    """POST a new in_progress check run. Returns its id."""
    payload = {
        "name": name,
        "head_sha": sha,
        "status": "in_progress",
        "started_at": now_iso(),
        "output": {"title": title or name, "summary": summary},
    }
    if details_url:
        payload["details_url"] = details_url

    result = run(
        ["gh", "api", "--method", "POST", f"repos/{repo}/check-runs", "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to create check run '{name}' on {repo}@{sha}: {result.stderr}"
        )
    body = json.loads(result.stdout)
    return body["id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/application-sdk"
    )
    parser.add_argument(
        "--sha", required=True, help="Head SHA to attach the check run to."
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Check run name, e.g. 'Connector E2E / atlan-mysql-app'.",
    )
    parser.add_argument("--title", default=None)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--details-url", default=None)
    args = parser.parse_args(argv)

    check_run_id = create_check_run(
        args.repo,
        args.sha,
        args.name,
        title=args.title,
        summary=args.summary,
        details_url=args.details_url,
    )

    line = f"check_run_id={check_run_id}"
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(line + "\n")
    else:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
