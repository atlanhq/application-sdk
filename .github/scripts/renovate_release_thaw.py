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

A failed dispatch is a warning, not a failure: the PR is re-evaluated on its
next CI completion anyway, and a red thaw would only ask someone to re-run
what a re-run would not fix. A listing that cannot be read does fail, because
then nothing was re-evaluated.

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

import renovate_approval_conditions as gate

#: Head-ref prefix of every Renovate branch (``branchPrefix`` default). The
#: gate re-checks the author, so this only bounds how many runs a thaw starts.
RENOVATE_BRANCH_PREFIX = "renovate/"

DEFAULT_APPROVE_WORKFLOW = "renovate-auto-approve.yml"


def open_renovate_prs(repo: str, runner: gate.Runner) -> list[str]:
    """Numbers of the open PRs on a ``renovate/`` branch, oldest first."""
    pulls = gate._gh_json(
        ["api", f"repos/{repo}/pulls?state=open&per_page=100", "--paginate", "--slurp"],
        runner,
        what="listing open PRs to thaw",
    )
    if not isinstance(pulls, list):
        raise gate.GhError("listing open PRs to thaw: unexpected payload shape")
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


def thaw(repo: str, mode: str, workflow: str, runner: gate.Runner) -> list[str]:
    """Dispatch one approval evaluation per open Renovate PR; return those sent."""
    if mode == gate.FREEZE_ON:
        print("RENOVATE_RELEASE_FREEZE=on — Renovate stays frozen; nothing to thaw.")
        return []
    if mode == gate.FREEZE_AUTO:
        trains = gate.releases_in_flight(repo, runner)
        if trains:
            print(
                f"Release train still in flight ({', '.join(trains)}) — not "
                "thawing; its own close will."
            )
            return []
    sent = []
    for pr in open_renovate_prs(repo, runner):
        result = gate._gh(
            ["workflow", "run", workflow, "--repo", repo, "-f", f"pr_number={pr}"],
            runner,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(f"::warning::PR #{pr}: could not dispatch {workflow}: {detail}")
            continue
        print(f"PR #{pr}: dispatched an approval re-evaluation ({workflow}).")
        sent.append(pr)
    if not sent:
        print("No open Renovate PRs re-evaluated.")
    return sent


def main(runner: gate.Runner = subprocess.run) -> int:
    repo = os.environ["REPO"]
    mode = gate.parse_freeze_mode(os.environ.get("RELEASE_FREEZE", ""))
    workflow = os.environ.get("APPROVE_WORKFLOW", "") or DEFAULT_APPROVE_WORKFLOW
    try:
        thaw(repo, mode, workflow, runner)
    except gate.GhError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
