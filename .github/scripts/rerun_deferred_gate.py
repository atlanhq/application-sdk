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
    payload = _load(
        run(["api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"])
    )
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return None
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

    candidates = newer_runs(repo, head_sha, self_run["workflow_id"], self_run_id, run)
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


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-run a newer Tests Gate that gave up waiting for the e2e verdict."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--head-sha", required=True, help="The PR head commit.")
    parser.add_argument("--self-run-id", required=True, type=int, help="github.run_id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    repair(args.repo, args.head_sha, args.self_run_id, dry_run=args.dry_run)
    # Always 0: a repair must never fail the gate job it runs in.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
