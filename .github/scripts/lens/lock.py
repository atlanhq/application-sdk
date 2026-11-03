"""One lens review per PR at a time — a new request is dropped, never queued.

GitHub's `concurrency:` can only queue a new run behind a running one, or
cancel the RUNNING one; neither is "keep the review in progress, refuse the
newcomer". So each run is named `lens #<pr>` (`run-name:` in lens.yml) and,
before doing anything else, asks the Actions API whether an OLDER lens run
for the same PR is still queued or running. If one is, this run exits
straight away: no model calls, no state change, a one-line note on the PR.

Only the younger of two runs yields, so two requests landing at the same
instant still resolve to exactly one review — never zero, never two.

A run's status alone is not trusted: GitHub can leave a run `in_progress`
after its only job has finished, which would refuse every `/lens` until
GitHub closes it. So an older run that looks active is checked against its
jobs: it blocks only while one of them is queued, or started no more than the
job timeout ago.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .github import GitHubError

WORKFLOW_FILE = "lens.yml"
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
# Must match `timeout-minutes:` of the review job in .github/workflows/lens.yml.
JOB_TIMEOUT = timedelta(minutes=45)


def run_name(pr: int) -> str:
    """Must match `run-name:` in .github/workflows/lens.yml."""
    return f"lens #{pr}"


def _started(job: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(job["started_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def _job_running(job: dict[str, Any], now: datetime) -> bool:
    if job.get("status") == "completed":
        return False
    started = _started(job)
    # `timeout-minutes` counts from when the job starts, not from when the run
    # was created: a job still queued for a runner can start at any time.
    return started is None or now - started <= JOB_TIMEOUT


def _still_running(
    run: dict[str, Any],
    jobs_of: Callable[[int], list[dict[str, Any]]],
    now: datetime,
) -> bool:
    try:
        jobs = jobs_of(int(run["id"]))
    except GitHubError as e:
        # Can't tell, so keep the guarantee of one review: yield, as before.
        print(f"::warning::lens: could not list jobs of run {run['id']}: {e}")
        return True
    # No jobs yet means the run is still being set up, not that it is done.
    return not jobs or any(_job_running(j, now) for j in jobs)


def older_active_run(
    runs: list[dict[str, Any]],
    pr: int,
    own_run_id: int,
    jobs_of: Callable[[int], list[dict[str, Any]]],
    now: datetime,
) -> dict[str, Any] | None:
    """The run this one must yield to, or None when it may proceed.

    `jobs_of` lists a run's jobs; it is only called for an older run that
    claims to be active."""
    name = run_name(pr)
    for r in runs:
        if r.get("id") == own_run_id or r.get("status") not in ACTIVE:
            continue
        if (r.get("display_title") or r.get("name")) != name:
            continue
        if int(r.get("id") or 0) >= own_run_id:  # run ids increase monotonically
            continue
        if _still_running(r, jobs_of, now):
            return r
    return None


BUSY_NOTE = (
    "lens is already reviewing this PR ({url}), so this request was ignored. "
    "Comment `/lens` again once that review has posted."
)
