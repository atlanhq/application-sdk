#!/usr/bin/env python3
"""Re-run a newer Tests Gate that gave up waiting for this run's e2e verdict.

A ``Tests`` run that did not act on the ``e2e`` label defers its gate to the
``e2e`` commit status the acting run writes (``e2e_commit_verdict.py``,
FND-3650). It waits for a ``pending`` status to resolve, but only for a bounded
time. An e2e run that outlasts that budget leaves the newer run's gate red, and
the newest check suite owns the required context (FND-2167), so the PR stays
blocked after its e2e has passed.

This driver runs in the acting run's gate job, after the verdict is recorded.
If the newest run on the commit is a completed run whose gate failed at the
deferral step, it re-runs that run's failed jobs. Only the gate job failed, so
only the gate job re-runs, and it reads the verdict that now exists. A re-read
of ``failure`` fails again, which is the correct outcome.

Safety properties, shared with ``rerun_evicted_tests_run.py``:

* **Fail open, always exit 0.** The gate's verdict is enforced elsewhere; an API
  error here leaves the PR as it was, with a ``::warning::``.
* **Only the newest run.** It is the one GitHub reads for the required context.
* **Only a completed run whose deferral step failed.** A run still in progress
  reads the verdict itself, and a gate that failed for any other reason (a unit
  failure) is not this driver's to retry.
* **One attempt.** A run already on attempt > 1 is left alone.

``GH_TOKEN`` needs ``actions: write`` (``ORG_PAT_GITHUB``), for the reason the
evicted-run driver documents. Without it the re-run is a logged no-op.

Unit-tested in tests/test_rerun_deferred_gate.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rerun_evicted_tests_run import (  # noqa: E402
    RunFn,
    _load,
    _notice,
    _run_gh,
    _warn,
    newer_runs,
)

#: The gate-job step that fails when the e2e verdict on the commit is not a
#: pass. Pinned against tests-reusable.yaml by the tests: a rename there would
#: silently stop this repair from recognising its target.
DEFER_STEP_NAME = "Enforce the e2e verdict on this commit"

#: The gate job's name suffix. In a called workflow the job is listed as
#: ``<caller job id> / Tests Gate``.
GATE_JOB_SUFFIX = "Tests Gate"


def gate_deferral_failed(repo: str, run_id: int, run: RunFn) -> bool | None:
    """Whether ``run_id``'s gate job failed at the deferral step.

    None means the jobs could not be read, which the caller treats as "leave it".
    """
    # Every page: a large e2e matrix can push the gate job past the first 100.
    payload = _load(
        run(
            [
                "api",
                f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
    )
    if not isinstance(payload, list):
        return None
    jobs: list = []
    for page in payload:
        page_jobs = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(page_jobs, list):
            return None
        jobs.extend(page_jobs)
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = job.get("name")
        if not isinstance(name, str) or not name.endswith(GATE_JOB_SUFFIX):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if (
                isinstance(step, dict)
                and step.get("name") == DEFER_STEP_NAME
                and step.get("conclusion") == "failure"
            ):
                return True
    return False


def repair(
    repo: str,
    head_sha: str,
    self_run_id: int,
    *,
    run: RunFn | None = None,
    dry_run: bool = False,
) -> bool:
    """Re-run the newest run's failed jobs if its gate deferred and gave up."""
    run = run or _run_gh

    self_run = _load(run(["api", f"repos/{repo}/actions/runs/{self_run_id}"]))
    if not isinstance(self_run, dict) or self_run.get("workflow_id") is None:
        _warn(f"could not read run {self_run_id}; skipping the deferred-gate repair")
        return False

    candidates = newer_runs(
        repo, head_sha, self_run["workflow_id"], self_run_id, run, event="pull_request"
    )
    if candidates is None:
        _warn(
            f"could not list the Tests runs on {head_sha[:7]}; leaving them as they are"
        )
        return False
    if not candidates:
        _notice("this run is the newest on the commit; no deferred gate to repair")
        return False

    newest = max(candidates, key=lambda entry: entry["id"])
    newest_id = newest["id"]
    if newest.get("status") != "completed":
        _notice(
            f"run {newest_id} is still running and will read the e2e verdict itself"
        )
        return False
    if newest.get("conclusion") != "failure":
        _notice(
            f"run {newest_id} concluded {newest.get('conclusion')!r}; nothing to repair"
        )
        return False
    attempt = newest.get("run_attempt") or 1
    if not isinstance(attempt, int) or attempt > 1:
        _notice(
            f"run {newest_id} is already on attempt {attempt}; re-running once is the cap"
        )
        return False

    deferred = gate_deferral_failed(repo, newest_id, run)
    if deferred is None:
        _warn(f"could not read the jobs of run {newest_id}; leaving it as it is")
        return False
    if not deferred:
        _notice(
            f"run {newest_id} failed for a reason other than the e2e verdict; leaving it"
        )
        return False

    if dry_run:
        _notice(f"--dry-run: would re-run the failed jobs of {repo} run {newest_id}")
        return True
    if not run(
        [
            "api",
            "-X",
            "POST",
            f"repos/{repo}/actions/runs/{newest_id}/rerun-failed-jobs",
        ]
    ):
        _warn(
            f"could not re-run run {newest_id}. Re-running needs `actions: write`, "
            "which only ORG_PAT_GITHUB carries here. Re-run its failed jobs by hand "
            "to pick up the e2e verdict."
        )
        return False
    _notice(
        f"re-ran the failed jobs of run {newest_id}; its gate now reads the e2e "
        "verdict this run recorded"
    )
    return True


#: Bound on waiting for a still-running PR run before reconciling its gate.
#: The gate is that run's last job and finishes within minutes of the rest.
RECONCILE_WAIT_SECONDS = 600
RECONCILE_POLL_SECONDS = 20

#: The PR run's gate job. A dispatched e2e-only run reports as `e2e / …`, so
#: the prefix keeps it from matching itself.
PR_GATE_JOB_NAME = "tests / Tests Gate"


def _pr_gate_job(repo: str, run_id: int, run: RunFn) -> dict | None:
    # Every page: a large e2e matrix can push the gate past the first 100 jobs,
    # and "not found" would leave a disagreeing gate unrepaired.
    payload = _load(
        run(
            [
                "api",
                f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&filter=latest",
                "--paginate",
                "--slurp",
            ]
        )
    )
    pages = payload if isinstance(payload, list) else [payload]
    for page in pages:
        jobs = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(jobs, list):
            return None
        for job in jobs:
            if isinstance(job, dict) and job.get("name") == PR_GATE_JOB_NAME:
                return job
    return None


def _deferral_failed(job: dict) -> bool:
    return any(
        isinstance(step, dict)
        and step.get("name") == DEFER_STEP_NAME
        and step.get("conclusion") == "failure"
        for step in job.get("steps") or []
    )


def gate_disagrees(job: dict, verdict: str) -> bool:
    """Whether a completed PR gate's result contradicts the e2e ``verdict``.

    * green, but e2e failed (or produced no verdict): the gate passed before
      the `/e2e` request existed, or read `pending`'s absence. It must re-read.
    * red only because it gave up waiting on `pending`, and e2e passed.

    A gate red for any other reason (a unit failure) is not e2e's to touch.
    """
    if job.get("status") != "completed":
        return False
    conclusion = job.get("conclusion")
    if conclusion == "success":
        return verdict in ("failure", "error")
    if conclusion == "failure":
        return verdict == "success" and _deferral_failed(job)
    return False


def reconcile(
    repo: str,
    head_sha: str,
    self_run_id: int,
    verdict: str,
    *,
    run: RunFn | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
    wait_seconds: int = RECONCILE_WAIT_SECONDS,
    dry_run: bool = False,
) -> bool:
    """From a dispatched e2e run, bring the PR run's Tests Gate in line with
    the ``verdict`` this run just recorded on ``head_sha`` (FND-3839).

    A `/e2e` comment dispatches e2e as its own run, so the PR's `pull_request`
    run — which owns the required `tests / Tests Gate` — may have concluded
    before the verdict existed. Its gate holds to the `e2e` status
    (e2e_commit_verdict.py), so re-running just that job makes it read the
    verdict. Driven by disagreement rather than an attempt cap: a re-run that
    reads the verdict agrees with it, so this cannot loop.
    """
    run = run or _run_gh
    self_run = _load(run(["api", f"repos/{repo}/actions/runs/{self_run_id}"]))
    if not isinstance(self_run, dict) or self_run.get("workflow_id") is None:
        _warn(f"could not read run {self_run_id}; skipping the gate reconcile")
        return False
    deadline = clock() + wait_seconds
    while True:
        runs = newer_runs(
            repo, head_sha, self_run["workflow_id"], 0, run, event="pull_request"
        )
        if runs is None:
            _warn(f"could not list the PR runs on {head_sha[:7]}; leaving them")
            return False
        if not runs:
            _notice("no pull_request run on this commit; nothing to reconcile")
            return False
        newest = max(runs, key=lambda entry: entry["id"])
        if newest.get("status") == "completed":
            break
        if clock() >= deadline:
            _warn(
                f"PR run {newest['id']} is still running after {wait_seconds}s; "
                "its gate reads the e2e status itself if it has not passed it yet. "
                "If it is green with a failed e2e, re-run its Tests Gate by hand."
            )
            return False
        sleep(RECONCILE_POLL_SECONDS)

    job = _pr_gate_job(repo, newest["id"], run)
    if job is None:
        _warn(f"could not find `{PR_GATE_JOB_NAME}` in run {newest['id']}")
        return False
    if not gate_disagrees(job, verdict):
        _notice(
            f"run {newest['id']}'s gate ({job.get('conclusion')}) already agrees "
            f"with the e2e verdict ({verdict})"
        )
        return False
    if dry_run:
        _notice(f"--dry-run: would re-run job {job['id']} of run {newest['id']}")
        return True
    if not run(["api", "-X", "POST", f"repos/{repo}/actions/jobs/{job['id']}/rerun"]):
        _warn(
            f"could not re-run the Tests Gate of run {newest['id']}. Re-running needs "
            "`actions: write` (ORG_PAT_GITHUB). Re-run it by hand to pick up the "
            "e2e verdict."
        )
        return False
    _notice(
        f"re-ran the Tests Gate of PR run {newest['id']}; it now reads the e2e "
        f"verdict ({verdict})"
    )
    return True


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-run a newer Tests Gate that gave up waiting for the e2e verdict."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--head-sha", required=True, help="The PR head commit.")
    parser.add_argument("--self-run-id", required=True, type=int, help="github.run_id")
    parser.add_argument(
        "--verdict",
        default="",
        help="The e2e verdict this run recorded. Set on a dispatched e2e run "
        "(FND-3839): the PR run's Tests Gate is then reconciled with it instead.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.verdict:
        reconcile(
            args.repo,
            args.head_sha,
            args.self_run_id,
            args.verdict,
            dry_run=args.dry_run,
        )
        return 0
    repair(args.repo, args.head_sha, args.self_run_id, dry_run=args.dry_run)
    # Always 0: a repair must never fail the gate job it runs in.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
