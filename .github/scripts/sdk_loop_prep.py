#!/usr/bin/env python3
"""Branch and check hygiene, before the first review reads anything.

This exists because of a boundary the review lane cannot cross. The review
phase holds a token with NO write scope — that is the whole read-only
guarantee — so it can neither update a branch that has fallen behind nor
re-run a check that flaked. Asking it to look at CI produced a fact it was
powerless to act on, reported beside a verdict that fact was explicitly not
allowed to influence.

So the duty moves here, to a phase that holds write scope, and runs once
before Review 1.

DETERMINISTIC FIRST, and usually deterministic ONLY. Everything this phase
normally does — read merge state, read failing checks — is a `gh` call with
an unambiguous answer. A model adds nothing to
"is mergeStateStatus BEHIND", and a clean PR is the common case, so paying an
agent to confirm a clean PR is clean would be the same waste this lane has
been trying to remove. The agent is invoked only when the deterministic pass
leaves something red that a mechanical fix might clear.

WHAT IT DELIBERATELY DOES NOT DO:

  * Touch the branch on its own initiative. Neither a conflict resolution
    nor a base merge is the loop's call to make: both are changes to
    somebody's PR that they did not ask for. Neither is needed to review
    either — the review reads the diff against base, which is well-defined
    whether or not base has moved. Both are REPORTED and left alone.
  * Wait for green. "Wait until CI passes" has no terminating condition when
    a check is genuinely broken by the PR, and the failure mode is a phase
    that burns an hour discovering what the review would have said in one
    line. Red CI is a fact to hand forward, not this phase's problem to
    solve.
  * Fix real test failures. That is the resolve phase's job, informed by a
    review. Prep clears MECHANICAL red only — formatting, lint, generated
    drift — the class where the fix is determined by the tooling rather than
    by judgement.

Environment:
    REPO, PR_NUMBER, HEAD_REF, BASE_SHA, GH_TOKEN
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

#: Checks whose failure is mechanical often enough to be worth one automatic
#: re-run. Everything else is left alone: re-running a genuine test failure
#: just spends CI minutes to reach the same answer more slowly.
RERUNNABLE_HINTS = ("flake", "timeout", "network", "rate limit")

OUTCOME_CLEAN = "clean"
OUTCOME_UPDATED = "updated"
OUTCOME_CONFLICTS = "conflicts"
OUTCOME_RED = "red"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True)
class PrepResult:
    outcome: str
    new_base_sha: str = ""
    #: Set ONLY when this phase pushed. Review 1 receives it as `ours`, so its
    #: own head check reads our update as our progress rather than as somebody
    #: else's commit — without it, a prep that did its job re-aims round 1
    #: every single time.
    pushed_sha: str = ""
    ci_state: str = "unknown"
    detail: str = ""
    failing: tuple[str, ...] = field(default_factory=tuple)


def _sh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def pr_state(repo: str, pr: int, runner=_sh) -> dict[str, str]:
    """Merge state and head sha, straight from the API."""
    done = runner(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "mergeStateStatus,headRefOid,mergeable",
        ]
    )
    try:
        payload = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return {k: str(v) for k, v in payload.items()}


def failing_checks(repo: str, pr: int, runner=_sh) -> tuple[str, ...]:
    """Named failing checks. Read ONCE — this is a fact, not a poll."""
    done = runner(
        [
            "gh",
            "pr",
            "checks",
            str(pr),
            "--repo",
            repo,
            "--json",
            "name,conclusion",
        ]
    )
    try:
        rows = json.loads(done.stdout or "[]")
    except json.JSONDecodeError:
        return ()
    return tuple(
        str(r.get("name", ""))
        for r in rows
        if isinstance(r, dict) and r.get("conclusion") == "failure"
    )


def decide(state: dict[str, str], failing: tuple[str, ...], before: str) -> PrepResult:
    """What prep concluded, from facts alone. No model involved.

    Conflicts short-circuit: there is nothing useful to say about checks on a
    branch that cannot merge, and the review posts NEEDS_REBASE regardless.

    A branch that is merely BEHIND is REPORTED, never updated. Updating it
    means pushing a merge commit into someone's PR on the loop's initiative,
    which is a change to their branch they did not ask for — and it is not
    needed to review: the diff against base is what the review reads, and
    that is well-defined whether or not base has moved. `mergeStateStatus`
    lands in the detail line so a human can act on it if they want to.
    """
    merge_state = state.get("mergeStateStatus", "")
    head = state.get("headRefOid", "") or before

    if merge_state == "CONFLICTING" or state.get("mergeable") == "CONFLICTING":
        return PrepResult(
            OUTCOME_CONFLICTS,
            new_base_sha=head,
            ci_state="unknown",
            detail="branch conflicts with base — the author resolves this, not the loop",
        )

    behind = (
        " · branch is behind base (reported, not updated)"
        if merge_state == "BEHIND"
        else ""
    )

    if failing:
        return PrepResult(
            OUTCOME_RED,
            new_base_sha=head,
            ci_state="red",
            detail=f"{len(failing)} failing check(s) — a fact for the review, not a blocker{behind}",
            failing=failing,
        )
    return PrepResult(
        OUTCOME_CLEAN,
        new_base_sha=head,
        ci_state="green",
        detail=f"checks green{behind or ' and branch is current'} — nothing to do",
    )


def needs_agent(result: PrepResult) -> bool:
    """Whether this run has anything a model could usefully act on.

    The answer is normally NO, and that is the point: a clean PR must cost
    zero model calls. Conflicts are excluded deliberately — they need a human,
    and handing them to an agent with write scope invites exactly the
    unrequested merge commit the playbook forbids.
    """
    return result.outcome == OUTCOME_RED and bool(result.failing)
