"""Find the run whose e2e preview files are this PR's comparison baseline.

The baseline is a successful run of the same workflow, in the same repo, on a
release (`bump-version*`) branch: those PRs carry the default branch's code and
run the full-DAG e2e as their gate, so their per-type asset counts are what the
connector landed at that point. ``render_preview_links.py`` diffs this PR's
counts against them.

Of those, it is the newest one cut at or before this PR's merge base with the
default branch. A release cut later carries default-branch changes the PR does
not have, and their effect on the counts would read as the PR's. The output
says how many default-branch commits separate the baseline's cut from the
merge base (0 = the same commit), so the comment can say how close it is.

Usage:
    python3 preview_baseline_run.py <owner/repo> <current-run-id>

Reads ``GITHUB_TOKEN`` and ``GITHUB_API_URL``. Prints ``run-id=<id>`` and
``commits-behind=<n>`` lines for ``$GITHUB_OUTPUT``, or nothing when there is
no baseline. Never fails the caller: any error prints nothing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable

RELEASE_BRANCH_PREFIX = "bump-version"
# Each candidate costs two compare calls; the merge base is rarely more than a
# few releases back.
MAX_CANDIDATES = 10
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
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


def candidates(runs: list[dict], current_run_id: int) -> list[dict]:
    """Successful release-branch runs other than this one, newest first."""
    return [
        run
        for run in runs
        if run.get("id") != current_run_id
        and run.get("conclusion") == "success"
        and str(run.get("head_branch", "")).startswith(RELEASE_BRANCH_PREFIX)
    ][:MAX_CANDIDATES]


def pick(
    runs: list[dict],
    current_run_id: int,
    behind: Callable[[dict], int | None],
) -> tuple[int, int] | None:
    """``(run id, commits behind)`` for the newest release run at or before
    the merge base.

    *behind* gives, for a run, how many default-branch commits its cut point
    is behind this PR's merge base, or ``None`` when it is not an ancestor of
    the merge base (cut later, on another line, or unreadable).
    """
    for run in candidates(runs, current_run_id):
        n = behind(run)
        if n is not None:
            return int(run["id"]), n
    return None


def _merge_base(api: str, repo: str, base: str, head: str, token: str) -> str | None:
    compare = _get(
        f"{api}/repos/{repo}/compare/{urllib.parse.quote(base, safe='')}...{head}",
        token,
    )
    sha = (compare.get("merge_base_commit") or {}).get("sha", "")
    return sha if _SHA.match(sha) else None


def _behind_by(api: str, repo: str, older: str, newer: str, token: str) -> int | None:
    """Commits *newer* is ahead of *older*, or ``None`` when *older* is not its
    ancestor."""
    compare = _get(f"{api}/repos/{repo}/compare/{older}...{newer}", token)
    if compare.get("status") == "identical":
        return 0
    if compare.get("status") == "ahead":
        return int(compare.get("ahead_by", 0))
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
        run = _get(f"{api}/repos/{repo}/actions/runs/{current}", token)
        default = str(_get(f"{api}/repos/{repo}", token)["default_branch"])
        head = str(run.get("head_sha", ""))
        if not _SHA.match(head):
            return 0
        pr_base = _merge_base(api, repo, default, head, token)
        if pr_base is None:
            return 0
        runs = _get(
            f"{api}/repos/{repo}/actions/workflows/{int(run['workflow_id'])}/runs"
            "?status=success&per_page=100",
            token,
        ).get("workflow_runs", [])
    # A baseline is a nicety; no answer is a comment without the diff, not a failure.
    except Exception:  # noqa: BLE001
        return 0

    def behind(candidate: dict) -> int | None:
        sha = str(candidate.get("head_sha", ""))
        if not _SHA.match(sha):
            return None
        try:
            cut = _merge_base(api, repo, default, sha, token)
            return None if cut is None else _behind_by(api, repo, cut, pr_base, token)
        # One unreadable candidate is skipped, not the whole baseline.
        except Exception:  # noqa: BLE001
            return None

    found = pick(runs, current, behind)
    if found is not None:
        print(f"run-id={found[0]}")
        print(f"commits-behind={found[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
