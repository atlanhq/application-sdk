#!/usr/bin/env python3
"""Comment on the merged PR whose release run failed (FND-4716).

conformance-release.yml and contract-toolkit-release.yml run when a PR that
touches their package merges. When one fails, no bump PR is opened or updated,
and every later change to that package is held back until someone notices.
Before this, nobody was notified: the gap was found only because a bump PR that
was expected never appeared.

The workflow calls this from an ``if: failure()`` step. It posts one comment on
the merged PR that fired the run, which notifies that PR's author and
subscribers and links the failed run. A ``workflow_dispatch`` run has no PR;
whoever dispatched it is watching the run, so the script only logs.

Environment:
    PR_NUMBER          the merged PR's number; empty on workflow_dispatch.
    RUN_URL            the failed run's URL.
    GITHUB_REPOSITORY  owner/repo.
    GH_TOKEN           a token with pull-requests: write.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Callable, Sequence

Runner = Callable[[Sequence[str], str], int]


def _gh(argv: Sequence[str], stdin: str) -> int:
    return subprocess.run(list(argv), input=stdin, text=True, check=False).returncode


def comment_body(package: str, workflow: str, run_url: str) -> str:
    return "\n".join(
        [
            f"### ⚠️ The {package} release run for this merge failed",
            "",
            f"[{workflow}]({run_url}) did not open or update the {package} release "
            f"PR, so this change, and any later {package} change, will not be "
            "released until it is fixed. The failed step's log says why.",
            "",
            "Once main is fixed, a merge that touches the package runs the release "
            f"again. You can also dispatch it from main: `gh workflow run {workflow}`.",
        ]
    )


def main(
    argv: Sequence[str] | None = None,
    env: dict[str, str] | None = None,
    gh: Runner = _gh,
) -> int:
    parser = argparse.ArgumentParser(
        description="Comment on the merged PR whose release run failed."
    )
    parser.add_argument("--package", required=True, help="name shown in the comment")
    parser.add_argument("--workflow", required=True, help="the workflow's file name")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else env

    pr = env.get("PR_NUMBER", "").strip()
    run_url = env.get("RUN_URL", "").strip()
    if not pr:
        print(f"No triggering PR (manual run): see {run_url}", flush=True)
        return 0
    body = comment_body(args.package, args.workflow, run_url)
    code = gh(
        [
            "gh", "pr", "comment", pr,
            "--repo", env["GITHUB_REPOSITORY"],
            "--body-file", "-",
        ],
        body,
    )  # fmt: skip
    if code != 0:
        print(f"::error::could not comment on PR #{pr} (gh exited {code})", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
