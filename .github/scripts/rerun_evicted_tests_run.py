#!/usr/bin/env python3
"""Re-run the newest `Tests` run on this commit when it was evicted.

## The failure this repairs

A bot PR receives several `pull_request` events on ONE head SHA within a second
or two — `opened` plus one `labeled` per label Renovate applies. Every consumer's
`tests.yaml` subscribes to `labeled` (the `e2e` label needs it), so GitHub
creates one `Tests` run per event on the same SHA. The `unit` job's concurrency
group is keyed on the PR ref with `cancel-in-progress: true`, so all but one of
those runs lose their `unit` job to an eviction ~1s in.

`tests-passed` is `if: always()`, deliberately — a required context must always
report (a called workflow that reports nothing leaves the PR pending forever).
So each evicted run publishes a **`failure`** `tests / Tests Gate` check run
alongside the surviving run's `success`.

That is survivable only while the surviving run is the newest one, because
**GitHub resolves a required status check to the check run of the NEWEST check
suite on the commit** — not the most recently completed one. Verified on the
`atlan-trino-app` fleet (FND-2167): across 18 PRs, every merged PR had its
highest-run-id gate `success`, and the one PR whose `success` sat at a *lower*
run id than a `cancelled` sibling was `mergeStateStatus: BLOCKED` with native
auto-merge already enabled — stuck until Renovate happened to rebase it.

Eviction order is not run-creation order, so which run survives is luck. Roughly
one bot PR in ten lands in the blocked shape.

## The repair, and why it is a re-run

This driver runs in the gate job of the run that *did* produce a real verdict.
If a NEWER run on the same commit concluded `cancelled`, that newer run owns the
required context and is holding a verdict no test produced — so we re-run it. A
re-run's check runs supersede the prior attempt's on the same context, and the
re-run genuinely executes the tests, so the authoritative verdict ends up being
a measured one.

The tempting cheaper fixes are both wrong, and worth recording so they are not
re-proposed:

* **Skipping the jobs on a non-`e2e` `labeled` event** (so the duplicate runs
  report nothing). A skipped job does not report nothing — it publishes a check
  run with conclusion `skipped`, and GitHub treats `skipped` as a **pass** for a
  required status check. The label runs are the newest suites, so their `skipped`
  gate would override a genuine `failure` from the `opened` run, and the Tests
  Gate would become decorative on exactly the auto-merge path it exists to guard.
* **Greening the gate when its only non-passing results are cancellations.** A
  human cancelling the only run would then green the required check with no test
  having run at all.

## Safety properties

* **Fail open, always exit 0.** This is a repair, not a gate; the gate's own
  verdict is computed and enforced by `verify-test-gate` regardless of what
  happens here. Any API error, missing token or unparseable payload leaves the
  PR exactly as it was — blocked, with a `::warning::` naming the cause.
* **It only ever re-runs a run that is already `completed` and `cancelled`**, so
  it cannot disturb live work.
* **It ignores a deliberate cancel.** An eviction is over in seconds (the job
  never gets a runner); a human cancelling a run they are watching does so
  minutes in. Candidates whose whole run lasted longer than
  ``--eviction-window-seconds`` are left alone.
* **One attempt only.** A candidate already on `run_attempt` > 1 is skipped, so
  two runs cannot re-run each other indefinitely.

## Token

`GH_TOKEN` must be able to WRITE `actions` to re-run (`ORG_PAT_GITHUB` in every
consumer). The reusable's `GITHUB_TOKEN` cannot be raised to `actions: write`:
a called workflow may only equal or narrow its caller's grant, and the callers
grant `actions: read`. Requesting more is a hard workflow error, which would red
every consumer at once — so the PAT is the only route, and its absence is a
no-op rather than a failure.

Extracted from inline shell per docs/standards/ci.md; unit-tested in
tests/test_rerun_evicted_tests_run.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable

RunFn = Callable[[list], str]

#: Conclusion of a run that never produced a verdict.
_CANCELLED = "cancelled"
_COMPLETED = "completed"

#: A run cancelled within this many seconds of starting was evicted by the
#: concurrency group rather than cancelled by a person. An eviction never gets a
#: runner (`gh api .../jobs/<id>/logs` → 404) and the whole run is over in well
#: under a minute; the observed FND-2167 storm took 27s from creation to the
#: gate's own failure. A human watching a run cancels it minutes in, and that
#: cancel must stick.
_EVICTION_WINDOW_SECONDS = 180

#: How long to keep looking while the newest run on the commit is still
#: settling. The gate of a fast run can reach this driver while a sibling is
#: mid-eviction, and "in progress" is the one state we cannot decide from. An
#: eviction completes in seconds, so this budget is generous; a genuinely
#: running newer run outlasts it and is correctly left to post its own verdict.
_SETTLE_BUDGET_SECONDS = 120
_POLL_INTERVAL_SECONDS = 10


def _run_gh(args: list) -> str:
    """Run `gh` and return stdout, or "" on any failure.

    The single seam the tests stub (docs/standards/ci.md), with `gh`'s stderr
    echoed as a `::warning::` so an auth/scope error stays diagnosable instead of
    collapsing silently into the fail-open path.
    """
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        if result.stderr:
            print(
                f"::warning::gh {' '.join(args[:2])} failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
        return ""
    return result.stdout


def _load(raw: str):
    """Parse `gh` JSON output, returning None on an empty/invalid payload."""
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("::warning::could not parse the runs payload as JSON", file=sys.stderr)
        return None


def _notice(message: str) -> None:
    # stderr, like every annotation in these drivers: stdout is reserved for a
    # `key=value` contract when a caller redirects it into $GITHUB_OUTPUT, and a
    # bare line there fails the step.
    print(f"::notice::{message}", file=sys.stderr)


def _warn(message: str) -> None:
    print(f"::warning::{message}", file=sys.stderr)


def _parse_timestamp(value: object) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (`2026-09-16T14:28:56Z`)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def run_duration_seconds(run: dict) -> float | None:
    """Wall-clock seconds the run existed, or None when it cannot be derived.

    ``run_started_at`` is absent on a run that never started, which is precisely
    the eviction case, so ``created_at`` is the fallback rather than an error.
    None is returned only when neither end is parseable — and the caller treats
    None as "cannot prove this was an eviction", i.e. leaves the run alone.
    """
    started = _parse_timestamp(run.get("run_started_at")) or _parse_timestamp(
        run.get("created_at")
    )
    ended = _parse_timestamp(run.get("updated_at"))
    if started is None or ended is None:
        return None
    return max((ended - started).total_seconds(), 0.0)


def is_repairable_eviction(run: dict, eviction_window: int) -> tuple[bool, str]:
    """Whether ``run`` is a completed eviction worth re-running.

    Returns the decision and the human-readable reason, so the caller logs the
    same sentence the test asserts on — a skip that cannot be explained in the
    run log is a skip nobody will debug.
    """
    if run.get("status") != _COMPLETED:
        return False, f"still {run.get('status')!r}; it will post its own verdict"
    conclusion = run.get("conclusion")
    if conclusion != _CANCELLED:
        return False, f"concluded {conclusion!r}, so it owns a real verdict"
    attempt = run.get("run_attempt") or 1
    if not isinstance(attempt, int) or attempt > 1:
        return (
            False,
            f"already on attempt {attempt}; re-running once is the cap, so that a "
            "pair of runs cannot re-run each other indefinitely",
        )
    duration = run_duration_seconds(run)
    if duration is None:
        return False, "its timestamps are unreadable, so eviction cannot be proven"
    if duration > eviction_window:
        return (
            False,
            f"it ran for {duration:.0f}s (> {eviction_window}s), which reads as a "
            "deliberate cancel rather than a concurrency-group eviction",
        )
    return True, f"cancelled {duration:.0f}s in, the signature of an eviction"


def newer_runs(
    repo: str,
    head_sha: str,
    workflow_id: object,
    self_run_id: int,
    run: RunFn,
) -> list | None:
    """Runs of the same workflow on ``head_sha`` that outrank ``self_run_id``.

    "Outrank" is `id` order, because that is what GitHub's required-check
    resolution follows: the newest check suite on the commit owns the context.
    None (not an empty list) signals "could not read", which the caller must not
    confuse with "there are none".
    """
    payload = _load(
        run(
            [
                "api",
                f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
    )
    pages = payload if isinstance(payload, list) else [payload]
    runs: list = []
    for page in pages:
        if not isinstance(page, dict):
            return None
        entries = page.get("workflow_runs")
        if not isinstance(entries, list):
            return None
        runs.extend(entries)
    return [
        entry
        for entry in runs
        if isinstance(entry, dict)
        and entry.get("workflow_id") == workflow_id
        and isinstance(entry.get("id"), int)
        and entry["id"] > self_run_id
    ]


def select_candidate(
    repo: str,
    head_sha: str,
    workflow_id: object,
    self_run_id: int,
    *,
    run: RunFn,
    eviction_window: int = _EVICTION_WINDOW_SECONDS,
    settle_budget: int = _SETTLE_BUDGET_SECONDS,
) -> dict | None:
    """The newest run on the commit, if it is an eviction we should re-run.

    Only the newest matters: it is the one whose check run GitHub reads for the
    required context. Older evicted siblings publish red rows that are ugly and
    harmless, and re-running them would put them all back in one concurrency
    group to evict each other again.
    """
    deadline = time.monotonic() + max(settle_budget, 0)
    while True:
        candidates = newer_runs(repo, head_sha, workflow_id, self_run_id, run)
        if candidates is None:
            _warn(
                f"could not list the Tests runs on {head_sha[:7]} — leaving the "
                "commit as it is. Re-running the newest run by hand is the manual "
                "equivalent of this repair."
            )
            return None
        if not candidates:
            _notice(
                "this run is the newest on the commit, so it already owns the "
                "required Tests Gate context — nothing to repair"
            )
            return None

        newest = max(candidates, key=lambda entry: entry["id"])
        repairable, reason = is_repairable_eviction(newest, eviction_window)
        if repairable:
            _notice(
                f"run {newest['id']} is newer than this one and was {reason}. It "
                "owns the required Tests Gate context on this commit, so it is "
                "being re-run to replace that unearned verdict with a measured one."
            )
            return newest

        if newest.get("status") != _COMPLETED and time.monotonic() < deadline:
            # The one undecidable state. Wait it out: an eviction resolves in
            # seconds, and a genuinely running run outlasts the budget and is
            # then correctly left alone.
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue

        _notice(f"newest run {newest['id']} left alone: {reason}")
        return None


def rerun(repo: str, run_id: int, run: RunFn, *, dry_run: bool = False) -> bool:
    """Re-run ``run_id``. Returns whether GitHub accepted the request."""
    if dry_run:
        _notice(f"--dry-run: would re-run {repo} run {run_id}")
        return True
    if not run(["api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/rerun"]):
        _warn(
            f"could not re-run {repo} run {run_id}. Re-running needs `actions: "
            "write`, which only ORG_PAT_GITHUB carries here — the reusable's own "
            "GITHUB_TOKEN cannot be raised to it without a hard workflow error in "
            "every consumer. The pull request is unchanged: its required Tests "
            "Gate still shows the evicted run's verdict, and re-running that run "
            "by hand clears it."
        )
        return False
    _notice(
        f"re-ran {repo} run {run_id}; its fresh check runs will replace the "
        "evicted attempt's on `tests / Tests Gate`"
    )
    return True


def repair(
    repo: str,
    head_sha: str,
    self_run_id: int,
    *,
    run: RunFn | None = None,
    eviction_window: int = _EVICTION_WINDOW_SECONDS,
    settle_budget: int = _SETTLE_BUDGET_SECONDS,
    dry_run: bool = False,
) -> bool:
    """Re-run the newest run on ``head_sha`` if it was evicted. True if re-run.

    ``run`` is resolved at call time rather than bound as a default argument, so
    stubbing the module-level seam takes effect for callers that don't pass it.
    """
    run = run or _run_gh

    # The workflow identity has to come from the run itself. `github.workflow`
    # is a display name (two workflows may share it) and a reusable cannot see
    # the caller's workflow id any other way.
    self_run = _load(run(["api", f"repos/{repo}/actions/runs/{self_run_id}"]))
    if not isinstance(self_run, dict) or self_run.get("workflow_id") is None:
        _warn(
            f"could not read run {self_run_id} to learn which workflow it belongs "
            "to — skipping the evicted-run repair for this commit"
        )
        return False

    candidate = select_candidate(
        repo,
        head_sha,
        self_run["workflow_id"],
        self_run_id,
        run=run,
        eviction_window=eviction_window,
        settle_budget=settle_budget,
    )
    if candidate is None:
        return False
    return rerun(repo, candidate["id"], run, dry_run=dry_run)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Re-run the newest Tests run on this commit when it was cancelled by "
            "a concurrency-group eviction and is holding the required Tests Gate "
            "context."
        )
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--head-sha",
        required=True,
        help="The commit the runs belong to (github.event.pull_request.head.sha).",
    )
    parser.add_argument(
        "--self-run-id",
        required=True,
        type=int,
        help="This run's id (github.run_id) — the run that produced a verdict.",
    )
    parser.add_argument(
        "--eviction-window-seconds",
        type=int,
        default=_EVICTION_WINDOW_SECONDS,
        help=(
            "A cancelled run that lasted longer than this is treated as a "
            "deliberate cancel and left alone."
        ),
    )
    parser.add_argument(
        "--settle-budget-seconds",
        type=int,
        default=_SETTLE_BUDGET_SECONDS,
        help="How long to wait while the newest run on the commit is still running.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the decision without re-running anything.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    repair(
        args.repo,
        args.head_sha,
        args.self_run_id,
        eviction_window=args.eviction_window_seconds,
        settle_budget=args.settle_budget_seconds,
        dry_run=args.dry_run,
    )
    # Always 0. A repair that could fail the gate job it runs in would turn a
    # cosmetic flake into the outage it exists to prevent.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
