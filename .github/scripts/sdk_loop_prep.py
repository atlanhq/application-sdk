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

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field

import sdk_review_approve as approve

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
            "mergeStateStatus,headRefOid,mergeable,baseRefName",
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


# ---------------------------------------------------------------------------
# Fast track — a re-trigger over a contribution nobody changed
# ---------------------------------------------------------------------------
#
# A merge from base moves the head sha without changing a line the PR author
# wrote. `sdk-review-reset-on-push.yml` then strips the verdict labels and
# branch protection dismisses the approval, both correctly — they are keyed to
# a sha, and the sha moved. But the REVIEW is not keyed to a sha; it is keyed
# to a diff. Re-reading an identical diff to reach an identical verdict is the
# one cost in this lane with no upside at all.
#
# So prep asks a question the review cannot: has this PR's own contribution
# changed since the last verdict? When the answer is no, the verdict is
# re-stamped on the live head and the round chain never starts.
#
# It does not post its own approval. It posts a verdict comment, and
# `sdk-review-approve-on-verdict.yml` — which already accepts this App's
# login — does the approving under the one identity that is a CODEOWNER. That
# keeps a single approval path with a single set of guards, and spends no
# extra `atlan-ci` request beyond the one APPROVE.

OUTCOME_FAST_TRACK = "fast_track"

#: GitHub's compare endpoint caps `files` at 300 and paginates past it. A
#: truncated list cannot prove two contributions are identical, so a PR that
#: large falls through to a full review rather than being fast-tracked on
#: partial evidence.
COMPARE_FILE_CAP = 300

#: Reviewers that are not people. `type == "Bot"` catches the App identities;
#: `atlan-ci` is a PAT-backed *user* account, so it has to be named — it is
#: the identity the approval path itself posts under, and reading its own
#: approval as human activity would make the fast track decline every time it
#: had previously succeeded.
NON_HUMAN_REVIEWERS = frozenset({"atlan-ci"})


@dataclass(frozen=True)
class FastTrackDecision:
    """Whether the round chain can be skipped, and the sentence explaining it.

    `reason` is written to be read by the PR author, because it is: it goes
    into the comment when the fast track fires, and into the phase log when it
    does not. "Declined" is the common and unremarkable case — most loop runs
    are triggered because something really did change.
    """

    fires: bool
    reason: str
    head: str = ""
    reviewed_head: str = ""


def contribution_digest(repo: str, base_ref: str, head: str, runner=_sh) -> str | None:
    """Digest of the PR's OWN contribution at `head`, or None if unreadable.

    `compare/{base}...{head}` is the three-dot form: GitHub diffs `head`
    against its MERGE BASE with `base`, so the answer is what this branch
    contributes and nothing base has done since. That is the whole property
    the fast track rests on — a merge from base advances the head sha and the
    merge base together, leaving this digest untouched.

    The digest is over patch TEXT, deliberately, and not over the blob shas at
    head. A blob at head is the MERGED result, so a base-side edit anywhere in
    a file this PR also touches would flip it and decline to fast-track the
    exact case this exists for. Patch text moves only when base edits inside
    the context of one of our own hunks, which is worth re-reading.

    Fails closed. Every unreadable answer is None rather than an empty digest,
    because two empty digests compare equal and would fast-track a PR nobody
    successfully looked at. A file whose patch GitHub omits (binary, oversized,
    or mode-only) is unknown: a blob sha is content-only and the compare object
    has no mode, so hashing the blob would grant a fast track on a chmod that
    changed nothing GitHub can show. Unknown is a decline.
    """
    done = runner(["gh", "api", f"repos/{repo}/compare/{base_ref}...{head}"])
    if done.returncode != 0:
        return None
    try:
        payload = json.loads(done.stdout or "")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    files = payload.get("files")
    # A compare with no files is not a contribution this function can identify.
    # Returning a digest here would let two unrelated empty answers match.
    if not isinstance(files, list) or not files:
        return None
    if len(files) >= COMPARE_FILE_CAP:
        return None
    parts = []
    for entry in files:
        if not isinstance(entry, dict):
            return None
        patch = entry.get("patch")
        if not patch:
            # Omitted or empty: binaries, oversized files, chmod-only. No
            # mode field exists to distinguish those, so this file cannot
            # prove the contribution unchanged.
            return None
        parts.append(
            "\n".join(
                [
                    str(entry.get("filename") or ""),
                    str(entry.get("status") or ""),
                    str(patch),
                ]
            )
        )
    return hashlib.sha256("\x00".join(sorted(parts)).encode("utf-8")).hexdigest()


def _activity_after(stdout: str, when: str) -> bool | None:
    """Parse a timestamp/login/type TSV listing. None if a row is malformed."""
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            return None
        submitted, login, kind = fields[0], fields[1], fields[2]
        if kind == "Bot" or login in NON_HUMAN_REVIEWERS:
            continue
        # String compare is correct for ISO-8601 UTC timestamps, which is what
        # both the reviews and the comments endpoints return.
        if submitted and submitted > when:
            return True
    return False


def human_review_after(repo: str, pr: int, when: str, runner=_sh) -> bool | None:
    """Whether a person reviewed or commented after `when`. None if unread.

    ANY human review counts, not only CHANGES_REQUESTED — and so does a
    top-level PR comment. `sdk-review-dismiss-on-human.yml` dismisses the
    bot approval on both surfaces (`pull_request_review` and `issue_comment`);
    listing only `pulls/{pr}/reviews` would re-stamp READY_TO_MERGE over a
    comment that just dismissed it.

    Fails closed: None is returned on an unreadable listing and the caller
    treats it as "assume somebody did", because the cost of being wrong here
    is re-stamping an approval over a human's objection.
    """
    reviews = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr}/reviews",
            "--paginate",
            "--jq",
            '.[] | [(.submitted_at // ""), (.user.login // ""), (.user.type // "")] | @tsv',
        ]
    )
    if reviews.returncode != 0:
        return None
    saw = _activity_after(reviews.stdout, when)
    if saw is not False:
        return saw

    comments = runner(
        [
            "gh",
            "api",
            f"repos/{repo}/issues/{pr}/comments",
            "--paginate",
            "--jq",
            '.[] | [(.created_at // ""), (.user.login // ""), (.user.type // "")] | @tsv',
        ]
    )
    if comments.returncode != 0:
        return None
    return _activity_after(comments.stdout, when)


def fast_track_decision(
    *,
    repo: str,
    pr: int,
    head: str,
    base_ref: str,
    verdict_comment: dict | None,
    verdict: str | None,
    reviewed_head: str | None,
    ready: str,
    runner=_sh,
) -> FastTrackDecision:
    """Decide whether an unchanged contribution can carry its verdict forward.

    The verdict itself is passed in already extracted rather than parsed here,
    so this module never grows a second verdict parser alongside
    `sdk_review_approve.py`'s — one of those going stale against the other is
    how a lane starts approving on a verdict it misread.

    Every guard below declines rather than raises. A fast track that cannot
    prove itself is simply a normal loop run, which is the behaviour this
    repo had before the check existed.
    """
    if verdict_comment is None:
        return FastTrackDecision(
            False, "no previous verdict on this PR to carry forward"
        )
    if verdict != ready:
        return FastTrackDecision(
            False,
            f"the last verdict was {verdict or 'unreadable'}, not {ready} — "
            "its findings still stand",
        )
    if not reviewed_head:
        return FastTrackDecision(
            False, "the last verdict does not record which head it reviewed"
        )
    if not head:
        return FastTrackDecision(False, "could not read the PR's live head")

    created = str(verdict_comment.get("created_at") or "")
    if not created:
        return FastTrackDecision(False, "the last verdict has no timestamp to age")
    reviewed_since = human_review_after(repo, pr, created, runner=runner)
    if reviewed_since is None:
        return FastTrackDecision(
            False,
            "could not read the PR's reviews or comments — assuming a human has weighed in",
        )
    if reviewed_since:
        return FastTrackDecision(
            False,
            "a human commented or reviewed after the last verdict — their read wins",
        )

    # The cheap case, and a real one: the head never moved. The verdict still
    # describes it exactly, and the loop is being re-triggered because the
    # approval was lost rather than because anything changed.
    if reviewed_head == head:
        return FastTrackDecision(
            True,
            "the head has not moved since that verdict, so it still describes "
            "this PR exactly",
            head=head,
            reviewed_head=reviewed_head,
        )

    before = contribution_digest(repo, base_ref, reviewed_head, runner=runner)
    after = contribution_digest(repo, base_ref, head, runner=runner)
    if before is None or after is None:
        return FastTrackDecision(
            False, "could not read both diffs to compare them — reviewing properly"
        )
    if before != after:
        return FastTrackDecision(False, "the diff has changed since the last verdict")
    return FastTrackDecision(
        True,
        "every commit since that verdict came from base — this PR's own diff is "
        "byte-identical to the one already reviewed",
        head=head,
        reviewed_head=reviewed_head,
    )


def fast_track_comment(
    pr: int, decision: FastTrackDecision, ready: str, run_url: str = ""
) -> str:
    """The verdict comment that carries a review forward onto a new head.

    Shaped like every other verdict comment on purpose. `<!-- SDK_REVIEW -->`
    plus `<!-- VERDICT: ... -->` plus `<!-- REVIEWED_HEAD: ... -->` is the
    contract `sdk_review_approve.py`, the reconcile sweep and the dismiss and
    downgrade lanes all read, and an empty `### Findings` is what the loop's
    own summary checks to call a round merge-ready. The extra
    `SDK_LOOP_FAST_TRACK` marker is additive: it lets a human — or a later
    audit — tell a carried verdict from a freshly computed one, which reading
    the prose alone would not.
    """
    lines = [
        "<!-- SDK_REVIEW -->",
        f"<!-- VERDICT: {ready} -->",
        f"<!-- REVIEWED_HEAD: {decision.head} -->",
        "<!-- SDK_LOOP_FAST_TRACK -->",
        f"<!-- FAST_TRACKED_FROM: {decision.reviewed_head} -->",
        f"## SDK Review (@sdk-loop fast track): PR #{pr}",
        "",
        "### Verdict: READY TO MERGE",
        "",
        f"> Fast-tracked without a review: {decision.reason}.",
        "",
        "---",
        "",
        "### Findings",
        "",
        "---",
        "",
        f"Carried forward from the review of `{decision.reviewed_head[:8]}`, "
        f"re-stamped on `{decision.head[:8]}`.",
        "",
        "No model ran and nothing was re-read: the review that produced this "
        "verdict read the same diff, so reviewing it again could only reach "
        "the same answer more slowly. Push a change and the next `@sdk-loop` "
        "reviews it properly.",
    ]
    if run_url:
        lines += ["", f"**Run:** [view workflow logs]({run_url})"]
    return "\n".join(lines)


def fast_track_check(
    repo: str, pr: int, head: str, state: dict[str, str] | None
) -> FastTrackDecision | None:
    """Ask whether the last verdict can be re-stamped on `head`.

    Author-filtered on purpose, and NOT through the loop's own
    `is_verdict_comment`, which tests the marker alone. A marker is text any PR
    author can type, and the comment this leads to can drive an `atlan-ci`
    APPROVE — so the trusted-bot check in `sdk_review_approve` is the one that
    has to apply. That module is also where the verdict and head are parsed,
    so this lane never grows a second parser to drift against the first.

    None means "could not even ask" — no base branch to compare against.
    """
    base_ref = (state or {}).get("baseRefName", "")
    if not base_ref:
        print("fast track skipped: could not read the PR's base branch")
        return None
    client = approve.Client(repo, str(pr))
    comment = client.latest_summary_comment()
    body = (comment or {}).get("body") or ""
    return fast_track_decision(
        repo=repo,
        pr=pr,
        head=head,
        base_ref=base_ref,
        verdict_comment=comment,
        verdict=approve.extract_verdict(body),
        reviewed_head=approve.extract_reviewed_head(body),
        ready=approve.READY,
    )


def post_fast_track(
    repo: str, pr: int, fast: FastTrackDecision, run_url: str = ""
) -> bool:
    """Post the carried-forward verdict comment. True when it landed.

    Posted under this step's installation token, which is
    `atlan-app-fleet[bot]` — one of the two logins
    `sdk-review-approve-on-verdict.yml` accepts as a verdict author. That
    workflow, not this one, posts the approval: it holds the `atlan-ci`
    credential that satisfies CODEOWNERS and the guards that decide whether an
    approval is still warranted by the time it fires.
    """
    body = fast_track_comment(pr, fast, approve.READY, run_url)
    done = subprocess.run(
        ["gh", "pr", "comment", str(pr), "--repo", repo, "--body", body],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        print(
            f"::warning::could not post the fast-track verdict: {done.stderr.strip()}"
        )
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    """The deterministic pass, as a step of its own.

    Split out so the workflow can decide whether to install anything. Prep is
    normally a couple of `gh` reads and no model at all, but it was paying 18
    seconds to `npm install opencode` first — on every run, for an agent it
    almost never invokes. That is nearly half the phase spent preparing for a
    branch it does not take.

    Emits `needs_agent` so the workflow gates the install on it, and emits the
    full result too: when no agent is needed this step IS the phase, and the
    job can end here.
    """
    repo, pr = os.environ["REPO"], int(os.environ["PR_NUMBER"])
    baseline = os.environ.get("BASE_SHA", "")

    state = pr_state(repo, pr)
    # Conflicts short-circuit BEFORE the checks read: nothing useful can be
    # said about CI on a branch that cannot merge, and the read is a round
    # trip spent to reach an answer that changes nothing.
    conflicted = state is not None and (
        state.get("mergeStateStatus") == "CONFLICTING"
        or state.get("mergeable") == "CONFLICTING"
    )
    result = decide(state, () if conflicted else failing_checks(repo, pr), baseline)

    # Before spending a review: has this PR's own diff changed since the last
    # one? A merge from base moves the head sha and dismisses the approval
    # without touching a line the author wrote, and re-reading an identical
    # diff can only reach an identical verdict more slowly.
    #
    # This lives HERE, in the deterministic step, and not in the agent phase —
    # because on a clean PR this step IS the phase. The workflow gates the
    # opencode install and `sdk_loop_phase.py` on `needs_agent`, which is
    # false for a clean PR, so a fast track placed in the phase would never
    # execute for the exact case it exists for. Its first draft did that: the
    # tests were green and the feature could not fire.
    #
    # Gated on OUTCOME_CLEAN, which is doing real work rather than being tidy:
    # `decide` returns CLEAN only when the checks read succeeded and found
    # nothing failing. An unreadable check state is UNKNOWN, not CLEAN, so a
    # PR whose CI could not be read is reviewed properly instead of carried
    # forward on an assumption.
    if result.outcome == OUTCOME_CLEAN:
        fast = fast_track_check(repo, pr, result.new_base_sha, state)
        if fast is not None and fast.fires:
            if post_fast_track(repo, pr, fast, os.environ.get("GHA_RUN_URL", "")):
                result = PrepResult(
                    OUTCOME_FAST_TRACK,
                    new_base_sha=fast.head,
                    ci_state=result.ci_state,
                    detail=f"fast-tracked without a review — {fast.reason}",
                )
            else:
                # The comment IS the mechanism: it is what
                # `sdk-review-approve-on-verdict.yml` reads to post the
                # approval. If it did not land there is nothing to approve, so
                # fall through and review the PR rather than cancel the chain
                # on a fast track that left no trace.
                print("::warning::fast track was earned but the comment did not post")
        elif fast is not None:
            print(f"fast track declined: {fast.reason}")

    wants = needs_agent(result)

    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"needs_agent={'true' if wants else 'false'}\n")
            handle.write(f"outcome={result.outcome}\n")
            handle.write(f"new_base_sha={result.new_base_sha}\n")
            handle.write(f"pushed_sha={result.pushed_sha}\n")
            handle.write(f"ci_state={result.ci_state}\n")
            handle.write(f"detail={result.detail}\n")
            handle.write(f"failing={','.join(result.failing)}\n")

    print(f"prep: {result.outcome} — {result.detail}")
    if not wants:
        print("prep: nothing for a model to do — skipping the opencode install")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
