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
  * Wait for green, or re-run a check hoping for a different answer. "Wait
    until CI passes" has no terminating condition when a check is genuinely
    broken by the PR, and the failure mode is a phase that burns an hour
    discovering what the review would have said in one line. Red CI is a
    fact to hand forward, not this phase's problem to solve.
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

OUTCOME_CLEAN = "clean"
OUTCOME_UPDATED = "updated"
OUTCOME_CONFLICTS = "conflicts"
OUTCOME_RED = "red"
OUTCOME_FAILED = "failed"
#: Prep could not read the PR's state. NOT the same as "nothing wrong" — the
#: distinction this file previously lost.
OUTCOME_UNKNOWN = "unknown"


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


def pr_state(repo: str, pr: int, runner=_sh) -> dict[str, str] | None:
    """Merge state and head sha, or None when they could not be read.

    None rather than `{}` for the same reason `failing_checks` returns None:
    an empty dict reads as "no conflicts, not behind", which is the most
    optimistic possible reading of a failed API call.
    """
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
    if done.returncode != 0:
        return None
    try:
        payload = json.loads(done.stdout or "")
    except (json.JSONDecodeError, TypeError):
        return None
    # `headRefOid` is the field every caller depends on. A dict without it is
    # not a state this phase can reason about, and treating it as one means
    # `decide` falls back to the baseline sha and calls the PR clean — the
    # same optimistic collapse this whole function exists to avoid.
    if not isinstance(payload, dict) or not payload.get("headRefOid"):
        return None
    return {k: str(v) for k, v in payload.items()}


#: `gh pr checks --json` field names, pinned because getting them wrong is
#: SILENT. `conclusion` — the obvious guess, and what this file shipped with
#: — is not a field: gh prints "Unknown JSON field" to stderr, exits 0, and
#: writes nothing to stdout. A reader that trusts stdout then sees no failing
#: checks and reports green on a PR that is red. That is exactly what
#: happened, and the same idiom is still in the toolkit playbook.
CHECK_FIELDS = "name,state,bucket"

#: The bucket value gh uses for a failed check. The vocabulary is
#: pass / fail / skipping / pending, NOT the GitHub API's conclusion strings.
BUCKET_FAIL = "fail"


def failing_checks(repo: str, pr: int, runner=_sh) -> tuple[str, ...] | None:
    """Named failing checks, or None when the state could not be read.

    None is not the same as "nothing is failing", and collapsing the two is
    how this function shipped broken. It asked for a field gh does not have;
    gh exits 0 and prints nothing; `json.loads(stdout or "[]")` turned that
    into an empty list; and prep reported green. Every layer degraded quietly
    into the most optimistic answer.

    So this fails CLOSED: a non-zero exit, unparseable output, or a payload
    that is not a list all return None, and the caller says "unknown" rather
    than "green".
    """
    done = runner(
        ["gh", "pr", "checks", str(pr), "--repo", repo, "--json", CHECK_FIELDS]
    )
    if done.returncode != 0:
        return None
    try:
        rows = json.loads(done.stdout or "")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(rows, list):
        return None
    return tuple(
        str(r.get("name", ""))
        for r in rows
        if isinstance(r, dict) and r.get("bucket") == BUCKET_FAIL
    )


def decide(
    state: dict[str, str] | None,
    failing: tuple[str, ...] | None,
    before: str,
) -> PrepResult:
    """What prep concluded, from facts alone. No model involved.

    Both inputs are optional because both reads can fail, and an unread state
    must never render as a clean one. That collapse is precisely how the
    first version of this file reported green on a red PR.

    A branch that is merely BEHIND is REPORTED, never updated. Merging base
    into someone's PR is a change to their branch they did not ask for, and
    it is not needed to review: the review reads the diff against base, which
    is well-defined whether or not base has moved.
    """
    if state is None:
        return PrepResult(
            OUTCOME_UNKNOWN,
            new_base_sha=before,
            ci_state="unknown",
            detail="could not read PR state — reporting unknown rather than clean",
        )

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

    if failing is None:
        return PrepResult(
            OUTCOME_UNKNOWN,
            new_base_sha=head,
            ci_state="unknown",
            detail=f"could not read check state — reporting unknown, not green{behind}",
        )

    if failing:
        return PrepResult(
            OUTCOME_RED,
            new_base_sha=head,
            ci_state="red",
            detail=(
                f"{len(failing)} failing check(s) — a fact for the review, "
                f"not a blocker{behind}"
            ),
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
