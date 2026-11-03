#!/usr/bin/env python3
"""Re-evaluate the Renovate PRs a release freeze held, once the train ends (FND-4175).

``renovate_approval_conditions.py`` withholds the atlan-ci approval while a
release train is in flight (condition i). Withholding is all it does: the held
PR keeps auto-merge armed and waits for a review that nothing would ever post,
because the gate only re-fires on its PR's own CI completions. Only the
lock-maintenance and framework lanes rebase when main moves; every other lane
rebases only on conflict, so a bump-PR merge does not re-run its CI.

``renovate-release-thaw.yml`` runs this when a ``bump-version*`` PR closes,
which is when the train ends. For each open ``renovate/`` PR it dispatches the
caller's approval workflow with ``pr_number``, and the gate judges it as it
would any other run: every condition is re-checked there, including the freeze
itself. So dispatching for a PR that was never held, or while another bump PR's
train is still running, costs a short run and approves nothing it should not.

Nothing is dispatched while the freeze still holds (another lane's train, or
``RENOVATE_RELEASE_FREEZE=on``): each run would only hold the PR again. The
thaw for that other train re-evaluates them.

A failed dispatch is retried once, then fails the run. A held PR's checks
were already green when the gate held it, and most lanes do not rebase on a
bump merge, so no later CI completion is coming to re-evaluate it: a dropped
dispatch strands it until someone notices. The red run is that notice, and
re-running it re-dispatches every open Renovate PR, which is safe — the gate
does not approve the same commit twice. A listing that cannot be read fails
too, because then nothing was re-evaluated.

Environment:
    GH_TOKEN         token with actions: write (dispatch), pull-requests: read
                     and statuses: read.
    REPO             owner/name.
    RELEASE_FREEZE   the ``RENOVATE_RELEASE_FREEZE`` override, as the gate reads it.
    APPROVE_WORKFLOW the approval caller's workflow file
                     (default ``renovate-auto-approve.yml``).

Usage::

    REPO=atlanhq/application-sdk python3 .github/scripts/renovate_release_thaw.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable

import renovate_approval_conditions as gate

#: Head-ref prefix of every Renovate branch (``branchPrefix`` default). The
#: gate re-checks the author, so this only bounds how many runs a thaw starts.
RENOVATE_BRANCH_PREFIX = "renovate/"

DEFAULT_APPROVE_WORKFLOW = "renovate-auto-approve.yml"

#: Seconds before the one retry of a failed dispatch.
RETRY_DELAY_SECONDS = 10


def open_renovate_prs(repo: str, runner: gate.Runner) -> list[str]:
    """Numbers of the open PRs on a ``renovate/`` branch, oldest first."""
    pulls = gate._gh_list(
        ["api", f"repos/{repo}/pulls?state=open&per_page=100"],
        runner,
        what="listing open PRs to thaw",
    )
    numbers = [
        int(pull["number"])
        for pull in pulls
        if isinstance(pull, dict)
        and pull.get("number")
        and str((pull.get("head") or {}).get("ref") or "").startswith(
            RENOVATE_BRANCH_PREFIX
        )
    ]
    return [str(n) for n in sorted(numbers)]


def _dispatch(
    repo: str, workflow: str, pr: str, runner: gate.Runner
) -> subprocess.CompletedProcess:
    return gate._gh(
        ["workflow", "run", workflow, "--repo", repo, "-f", f"pr_number={pr}"],
        runner,
    )


def thaw(
    repo: str,
    mode: str,
    workflow: str,
    runner: gate.Runner,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[str], list[str]]:
    """Dispatch one approval evaluation per open Renovate PR.

    Returns ``(sent, failed)``: the PRs dispatched, and those whose dispatch
    still failed after one retry.
    """
    if mode == gate.FREEZE_ON:
        print("RENOVATE_RELEASE_FREEZE=on — Renovate stays frozen; nothing to thaw.")
        return [], []
    if mode == gate.FREEZE_AUTO:
        trains = gate.releases_in_flight(repo, runner)
        if trains:
            print(
                f"Release train still in flight ({', '.join(trains)}) — not "
                "thawing; its own close will."
            )
            return [], []
    sent, failed = [], []
    for pr in open_renovate_prs(repo, runner):
        result = _dispatch(repo, workflow, pr, runner)
        if result.returncode != 0:
            sleep(RETRY_DELAY_SECONDS)
            result = _dispatch(repo, workflow, pr, runner)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(f"::error::PR #{pr}: could not dispatch {workflow}: {detail}")
            failed.append(pr)
            continue
        print(f"PR #{pr}: dispatched an approval re-evaluation ({workflow}).")
        sent.append(pr)
    if not sent and not failed:
        print("No open Renovate PRs to re-evaluate.")
    return sent, failed


def main(
    runner: gate.Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    repo = os.environ["REPO"]
    mode = gate.parse_freeze_mode(os.environ.get("RELEASE_FREEZE", ""))
    workflow = os.environ.get("APPROVE_WORKFLOW", "") or DEFAULT_APPROVE_WORKFLOW
    try:
        _, failed = thaw(repo, mode, workflow, runner, sleep)
    except gate.GhError as exc:
        print(f"::error::{exc}")
        return 1
    if failed:
        print(
            f"::error::{len(failed)} held Renovate PR(s) were not re-evaluated; "
            "re-run this workflow."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
