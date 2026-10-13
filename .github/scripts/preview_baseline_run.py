"""Find the run whose e2e preview files are this PR's comparison baseline.

The baseline is the latest successful run of the same workflow, in the same
repo, on a release (`bump-version*`) branch: those PRs carry the default
branch's code and run the full-DAG e2e as their gate, so their per-type asset
counts are what the connector lands today. ``render_preview_links.py`` diffs
this PR's counts against them.

Usage:
    python3 preview_baseline_run.py <owner/repo> <current-run-id>

Reads ``GITHUB_TOKEN`` and ``GITHUB_API_URL``. Prints the baseline run id, or
nothing when there is none. Never fails the caller: any error prints nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

RELEASE_BRANCH_PREFIX = "bump-version"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TIMEOUT_S = 20


def _get(url: str, token: str) -> dict:
    request = urllib.request.Request(  # noqa: S310 - fixed https API base
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
        return json.loads(response.read())


def pick(runs: list[dict], current_run_id: int) -> int | None:
    """The newest successful release-branch run that is not this run."""
    for run in runs:
        if (
            run.get("id") != current_run_id
            and run.get("conclusion") == "success"
            and str(run.get("head_branch", "")).startswith(RELEASE_BRANCH_PREFIX)
        ):
            return int(run["id"])
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 3 or not _REPO.match(argv[1]) or not argv[2].isdigit():
        return 0
    repo, current = argv[1], int(argv[2])
    token = os.environ.get("GITHUB_TOKEN", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    if not token or not api.startswith("https://"):
        return 0
    try:
        workflow_id = _get(f"{api}/repos/{repo}/actions/runs/{current}", token)[
            "workflow_id"
        ]
        runs = _get(
            f"{api}/repos/{repo}/actions/workflows/{int(workflow_id)}/runs"
            "?status=success&per_page=100",
            token,
        ).get("workflow_runs", [])
    # A baseline is a nicety; no answer is a comment without the diff, not a failure.
    except Exception:  # noqa: BLE001
        return 0
    found = pick(runs, current)
    if found is not None:
        print(found)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
