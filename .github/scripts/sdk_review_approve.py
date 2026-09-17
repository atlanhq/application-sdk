#!/usr/bin/env python3
"""Stamp an SDK review verdict onto a PR: labels, commit status, approval.

Driven by `sdk-review-approve-on-verdict.yml` when `mothership-ai[bot]` posts a
verdict comment. Decision logic lives here rather than inlined in the workflow,
per docs/standards/ci.md.

Two tokens, deliberately
------------------------
`atlan-ci` is a CODEOWNER (.github/CODEOWNERS), so the formal APPROVE review is
the one call that must carry that identity. Everything else here — resolving
HEAD, listing comments, reconciling labels, writing the commit status,
dismissing stale approvals — is identity-agnostic.

The previous shell implementation ran all of it on `atlan-ci`'s PAT, spending
~15 requests per fire against a single 5,000/hr user quota shared with every
other workflow that authenticates as `atlan-ci`. Under a burst of concurrent
review/resolve loops that quota ran out, and the APPROVE POST — the last call in
the sequence — took the 403. The review summary landed, the commit status went
green, and no approval was ever posted.

So: `GH_TOKEN` (a GitHub App installation token, its own quota) does everything,
and `APPROVER_TOKEN` (the `atlan-ci` PAT) is spent on exactly one request.

Ordering
--------
The approval is posted BEFORE the `sdk-review` commit status is set to success.
The old order wrote the status first, so a failed approval left the PR wearing a
green `sdk-review` check with no approving review — a state that reads as "the
bot is done" while the merge gate is still blocked. If the approval cannot be
posted the status is left as the dispatcher set it (pending) and the run fails
loudly.

A stale head is re-reviewed, not dropped
---------------------------------------
A verdict describes the sha stamped in its `REVIEWED_HEAD` marker. If the PR's
head has moved since, no stamp is applied — that guard is correct and must stay.
What was wrong was what happened next: nothing. The run exited green, the review
evaporated, and the loop sat waiting for a human who had no idea anything had
happened. So that one refusal now posts a single `@sdk-review` comment for the
current head (`request_rereview`), guarded once-per-sha so a fix->review->fix
chain cannot feed itself. The neighbouring refusals — no `REVIEWED_HEAD`, and an
unresolvable head — establish no sha to review and keep failing closed.

Labels
------
Written through the REST labels API rather than `gh pr edit`. Labels are an
Issues-scope resource; a fine-grained token carrying `pull_requests: write` but
not `issues: write` can approve a review yet silently fail every label write.
The old code sent label errors to /dev/null under `|| true`, so that failure was
invisible. Here a delete that 404s is tolerated (the label was already absent)
and anything else is surfaced as a ::warning::.

Three callers
-------------
The fast path (`sdk-review-approve-on-verdict.yml`) passes the verdict comment
in via COMMENT_BODY, straight off the `issue_comment` event, and owns the
`sdk-review` commit status.

The slow path (`sdk-review.yml`, after the mothership sandbox finishes its
cleanup) leaves COMMENT_BODY empty; the newest summary comment is fetched
instead. It sets EXPECTED_HEAD to the sha the review was dispatched for, does
not write the commit status (its workflow has its own failure-path step), and
sets REQUIRE_APPROVED_LABEL so it will not approve a verdict something has since
invalidated.

The reconciler (`sdk_review_reconcile.py`, on a cron) drives this per PR with the
same environment as the slow path, on a short retry budget: it exists to recover
approvals the other two paths lost, so waiting out a long rate-limit window
in-process would only duplicate the loop it already is. It calls
`stamp_verdict()` rather than `main()`, because it has to report whether an
approval was actually posted and an exit code cannot say.

That label guard is the solo-approval safety property. `sdk-review-approved` is
the one signal every invalidator clears: `dismiss-on-human` strips it (and
notably does NOT touch the commit status, so the status cannot substitute),
`downgrade-on-ci-failure` strips it, `reset-on-push` strips it. It is therefore
read from a snapshot taken BEFORE this run reconciles labels — the shell version
read it back after adding it, so the guard could only ever see the label it had
just written and never fired — and, when it fires on a READY verdict, it bails
before writing anything at all. Reconciling labels first would re-add
`sdk-review-approved` on the way to declining the approval, resurrecting the
signal the invalidator had just stripped.

Environment:
    REPO                        owner/repo (e.g. atlanhq/application-sdk)
    PR_NUMBER                   pull request number
    COMMENT_BODY                verdict comment body; when empty the newest
                                summary comment on the PR is fetched instead
    TRIGGERING_COMMENT_ID       id of the triggering comment; the freshness
                                check only applies when COMMENT_BODY is set
                                (fetching the newest makes it moot)
    EXPECTED_HEAD               optional; when set, the PR's live head must
                                match it as well as matching REVIEWED_HEAD
    WRITE_STATUS                true (default) | false — own the sdk-review
                                commit status
    REQUIRE_APPROVED_LABEL      false (default) | true — refuse to approve
                                unless `sdk-review-approved` was already on the
                                PR when this run started
    GH_TOKEN                    App installation token — every call but APPROVE
    APPROVER_TOKEN              `atlan-ci` PAT — the APPROVE call, and the
                                `@sdk-review` re-review request on a stale head
                                (the reviewer's `if:` admits no other identity
                                we hold). Absent, both are skipped.
    APPROVE_MAX_ATTEMPTS        default 3
    APPROVE_MAX_WAIT_SECONDS    total sleep budget across retries, default 120

Exit status (`main()`; in-process callers should read `stamp_verdict()`'s action
instead, which separates "approved" from "a guard declined"):
    0  stamped, or intentionally skipped by a guard
    1  the verdict could not be stamped: the approval failed, or the review
       listing could not be read and approving blind is not an option
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# The in-flight check the review gate already owns. Importing it rather than
# re-deriving it keeps one definition of "a run has claimed this sha and has
# not finished" — the two would otherwise drift, and a drifted copy either
# double-dispatches or never dispatches.
from sdk_review_gate import (  # noqa: E402  (needs the sys.path bootstrap)
    inflight_sibling_run,
)

Runner = Callable[..., subprocess.CompletedProcess]
Sleeper = Callable[[float], None]

APPROVED = "approved"
#: Mirrors sdk_loop_findings.MARK_AB. Duplicated rather than imported: this
#: script runs under `python3` on a bare runner and must stay import-free of
#: the loop lane, whose modules pull in PyYAML.
MARK_AB = "<!-- SDK_LOOP_AB -->"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(frozen=True)
class StampOutcome:
    """What a stamp run did, for callers that need more than an exit code.

    An exit status cannot separate "posted the approval" from "a guard declined
    to" — both are a successful run, and `main()` returns 0 for each. The
    reconciler needs that distinction to report an actual recovery rather than
    guess at one.

    Recovering it from a follow-up read does not work: GitHub's reviews listing
    is read-after-write eventually consistent, so an approval posted moments ago
    can be missing from the very next listing. The reconciler's first live run
    did exactly that — approved PR #3232, re-read, saw nothing, and reported
    that the stamper had declined. So the stamper says what it did instead of
    the caller inferring it.
    """

    action: str
    exit_code: int
    detail: str = ""


SUMMARY_MARKERS = ("<!-- SDK_REVIEW -->", "<!-- TEST_SDK_REVIEW -->")

# The logins whose verdict comments this script may act on. The fast path's
# job-level `if:` already pins the *triggering* comment to this set, but the
# script also re-lists every PR comment — to find the newest summary for
# supersede detection, and (on the slow path, COMMENT_BODY empty) to re-read the
# verdict itself. That read path must apply the same author check, or a forged
# `<!-- SDK_REVIEW -->` comment from anyone else would be treated as a verdict
# and could drive the atlan-ci APPROVE. Both entries are trusted bot identities
# owned by this org; neither can be assumed by a PR author.
#
# `atlan-app-fleet[bot]` is here because @sdk-loop posts its round verdicts
# under the fleet App token rather than through the mothership sandbox. While
# this was a single login, every consumer that FINDS a verdict by listing
# comments — `latest_summary_comment()`, and so `sdk_review_reconcile.py` —
# was blind to every loop verdict, and a loop approval lost to an `atlan-ci`
# rate limit could never be reconciled. `sdk-review-approve-on-verdict.yml`
# has accepted both logins in its `if:` since the loop shipped, so this
# constant was the outlier, not the change.
VERDICT_AUTHORS = frozenset({"mothership-ai[bot]", "atlan-app-fleet[bot]"})


def _is_verdict_comment(comment: dict) -> bool:
    """A comment counts as a verdict only from a reviewer bot, with a marker."""
    if (comment.get("user") or {}).get("login") not in VERDICT_AUTHORS:
        return False
    return any(marker in (comment.get("body") or "") for marker in SUMMARY_MARKERS)


VERDICT_RE = re.compile(r"<!--\s*VERDICT:\s*([A-Z_]+)\s*-->")
# Fallback for legacy comments predating the structured marker: "### Verdict:
# READY TO MERGE" in prose, which normalises to READY_TO_MERGE.
VERDICT_PROSE_RE = re.compile(r"###\s*Verdict:\s*([A-Za-z][A-Za-z ]*)", re.IGNORECASE)
REVIEWED_HEAD_RE = re.compile(r"<!--\s*REVIEWED_HEAD:\s*([0-9a-f]+)\s*-->")

READY = "READY_TO_MERGE"

APPROVED_LABEL = "sdk-review-approved"
NEEDS_HUMAN_LABEL = "sdk-review-needs-human"
NEEDS_REBASE_LABEL = "sdk-review-needs-rebase"

# Stripped unconditionally so PRs predating the promotion out of the
# `test-sdk-review-*` prefix don't end up wearing both.
LEGACY_LABELS = frozenset(
    {
        "test-sdk-review-approved",
        "test-sdk-review-needs-human",
        "test-sdk-review-needs-rebase",
    }
)

# The leading line of the approval body is a STABLE SIGNATURE used by
# sdk-review-dismiss-on-human.yml to tell bot approvals from human ones.
# Keep in sync with that workflow and with sdk-review.yml.
APPROVAL_SIGNATURE = "**SDK reviewer's verdict:**"
APPROVAL_BODY = (
    f"{APPROVAL_SIGNATURE} READY TO MERGE.\n"
    "\n"
    "Full review summary is in the comment posted on this PR.\n"
)


def extract_verdict(body: str) -> str | None:
    """The verdict, preferring the structured marker over the prose heading."""
    match = VERDICT_RE.search(body)
    if match:
        return match.group(1)
    prose = VERDICT_PROSE_RE.search(body)
    if not prose:
        return None
    # "READY TO MERGE" -> "READY_TO_MERGE"; trailing separators dropped.
    return "_".join(prose.group(1).upper().split()) or None


def extract_reviewed_head(body: str) -> str | None:
    """The sha the verdict was computed against, if the comment stamps one."""
    match = REVIEWED_HEAD_RE.search(body)
    return match.group(1) if match else None


# --- re-review request on a stale head -----------------------------------
#
# When the head has moved past the verdict, every stamp is refused — correctly:
# the verdict describes `reviewed_head`, and stamping the new head would bless
# code the reviewer never saw. Before FND-638 that was the end of it. The run
# exited green, the review evaporated, and the loop sat waiting for a human who
# had no idea anything had happened; six of sixteen completed runs on one day
# lost their verdict that way, at roughly $45 of review work.
#
# So the refusal now asks for one fresh review of the head that actually exists.
# The request has to be a comment because that is the only surface
# `sdk-review.yml` listens on, and its body has to START with `@sdk-review`
# because that workflow's job-level `if:` uses `startsWith`. The markers
# therefore sit at the bottom of the body, not the top.
RETRIGGER_MARKER = "<!-- SDK_REVIEW_RETRIGGER -->"
RETRIGGER_HEAD_RE = re.compile(
    r"<!--\s*SDK_REVIEW_RETRIGGER_HEAD:\s*([0-9a-f]{40})\s*-->"
)

# What `request_rereview` did, in words, for the ::warning:: it annotates. The
# slugs themselves are what `StampOutcome.detail` carries.
RETRIGGER_REASONS = {
    "posted": "requested a fresh review of the current head",
    "already-requested": "a re-review was already requested for this head",
    "already-reviewed": "the current head already has a verdict",
    "run-in-flight": "a review of the current head is already running",
    "comments-unreadable": "the PR comments could not be read to check",
    "no-approver-token": "no APPROVER_TOKEN to post the trigger with",
    "post-failed": "the trigger comment could not be posted",
}


def retrigger_body(stale_head: str, head_sha: str) -> str:
    """The `@sdk-review` comment that asks for a review of the current head."""
    return (
        "@sdk-review\n"
        "\n"
        f"The last review's verdict was computed for `{stale_head[:7]}`, but this "
        f"PR's head has since moved to `{head_sha[:7]}`, so that verdict cannot be "
        "stamped onto code it never saw. Requesting one fresh review of the "
        "current head.\n"
        "\n"
        f"{RETRIGGER_MARKER}\n"
        f"<!-- SDK_REVIEW_RETRIGGER_HEAD: {head_sha} -->\n"
    )


# The re-review trigger is posted as `atlan-ci` (see `request_rereview`), so a
# marker from any other author is not a request this loop made. Trusting one
# would let a forged marker for the current head — anyone can comment on a
# public-repo PR — read as `already-requested` and silently suppress the fresh
# review the stale-head refusal exists to ask for. Same fail-closed authorship
# rule `reviewed_at()` applies to verdicts.
RETRIGGER_AUTHOR = "atlan-ci"


def retrigger_posted_for(comments: list[dict], head_sha: str) -> bool:
    """Whether a re-review has already been requested for `head_sha`.

    Keyed on the sha rather than counted, so a fix->review->fix->review chain
    gets one request per distinct head and cannot feed itself: a second request
    for a sha that already has one is the loop, and this is where it stops.

    Only an `atlan-ci` marker counts: the request is only ever posted under
    that identity, so any other author's marker is either a human quoting one
    or a forgery, and neither proves the loop already asked.
    """
    for comment in comments:
        if (comment.get("user") or {}).get("login") != RETRIGGER_AUTHOR:
            continue
        body = comment.get("body") or ""
        if RETRIGGER_MARKER not in body:
            continue
        match = RETRIGGER_HEAD_RE.search(body)
        if match and match.group(1) == head_sha:
            return True
    return False


def reviewed_at(comments: list[dict], head_sha: str) -> bool:
    """Whether some verdict comment on this PR already reviewed `head_sha`.

    The fast path is driven by an `issue_comment` event, so the body it was
    handed can be an older verdict while a newer one covering the live head
    already sits on the PR. Asking for a review of a head that has one would be
    pure waste.
    """
    return any(
        _is_verdict_comment(comment)
        and extract_reviewed_head(comment.get("body") or "") == head_sha
        for comment in comments
    )


def label_plan(verdict: str, current: set[str]) -> tuple[set[str], set[str]]:
    """Return (to_add, to_remove) for `verdict`, given the PR's current labels.

    Only genuine deltas are returned, so an already-correctly-labelled PR costs
    zero label requests.
    """
    if verdict == READY:
        want, unwant = {APPROVED_LABEL}, {NEEDS_HUMAN_LABEL, NEEDS_REBASE_LABEL}
    elif verdict == "NEEDS_HUMAN":
        want, unwant = {NEEDS_HUMAN_LABEL}, {APPROVED_LABEL}
    elif verdict == "NEEDS_REBASE":
        want, unwant = {NEEDS_REBASE_LABEL}, {APPROVED_LABEL}
    else:
        # NEEDS_FIXES / BLOCKED / anything unrecognised: clear the two verdict
        # labels that would otherwise imply a resolved review. `needs-rebase` is
        # left alone — it describes the branch, not the verdict.
        want, unwant = set(), {APPROVED_LABEL, NEEDS_HUMAN_LABEL}

    unwant |= LEGACY_LABELS
    return want - current, (unwant & current)


def status_for(verdict: str) -> tuple[str, str]:
    """The (state, description) to publish as the `sdk-review` commit status."""
    if verdict == READY:
        return "success", "Approved"
    # NEEDS_FIXES / BLOCKED / NEEDS_HUMAN / NEEDS_REBASE / unknown all leave the
    # merge gate visibly blocked.
    return "failure", f"Verdict: {verdict}"


def is_rate_limited(stderr: str) -> bool:
    """Whether a failed `gh` invocation failed because of a rate limit."""
    lowered = stderr.lower()
    return "rate limit" in lowered or "secondary rate" in lowered


def is_secondary_rate_limit(stderr: str) -> bool:
    """Whether the failure was a *secondary* (abuse/concurrency) throttle.

    Secondary limits clear in seconds-to-a-minute and are worth waiting out on a
    bounded delay. Primary quota exhaustion is different: it holds until the
    hourly core window resets, which is what `rate_limit_reset()` reads — so the
    two must not share the same wait calculation (a secondary throttle against a
    distant core reset would otherwise fail fast instead of waiting its short
    window).
    """
    lowered = stderr.lower()
    return "secondary rate" in lowered or "abuse detection" in lowered


class Client:
    """Thin `gh api` wrapper. All calls use GH_TOKEN unless told otherwise."""

    def __init__(self, repo: str, pr_number: str, runner: Runner = subprocess.run):
        self.repo = repo
        self.pr_number = pr_number
        self._runner = runner

    def run(
        self, args: list[str], token: str | None = None
    ) -> subprocess.CompletedProcess:
        env = None
        if token:
            env = {**os.environ, "GH_TOKEN": token}
        return self._runner(
            ["gh", *args], capture_output=True, text=True, check=False, env=env
        )

    def head_sha(self) -> str | None:
        result = self.run(
            ["api", f"repos/{self.repo}/pulls/{self.pr_number}", "--jq", ".head.sha"]
        )
        if result.returncode != 0:
            print(f"::warning::could not resolve PR HEAD: {result.stderr.strip()}")
            return None
        return result.stdout.strip() or None

    def _paginated(self, path: str) -> list[dict] | None:
        """Flatten a `--paginate --slurp` response, or None if it could not be read.

        None and `[]` are deliberately different values. They used to be the
        same one, and that collapsed "the API did not answer" into "there is
        nothing there" — which is safe for a comment listing (no verdict means
        do nothing) and dangerous for a review listing, where "no approvals
        exist" is the precondition for posting one.

        Seen in production on 2026-08-17: GitHub's reviews endpoint began
        returning `404` (and, to other tokens, truncated JSON) for PRs that
        plainly had reviews. Every caller read that as an empty list, so the
        duplicate-approval guard was satisfied by an outage and `atlan-ci`
        re-approved the same PR once per reconciler tick.
        """
        result = self.run(["api", f"repos/{self.repo}/{path}", "--paginate", "--slurp"])
        if result.returncode != 0:
            print(f"::warning::could not list {path}: {result.stderr.strip()}")
            return None
        try:
            pages = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            print(f"::warning::could not parse {path} ({exc})")
            return None
        items: list[dict] = []
        for page in pages:
            # An un-paginated response is a bare array; --slurp adds one level.
            # Tolerate both shapes.
            if isinstance(page, list):
                items.extend(item for item in page if isinstance(item, dict))
            elif isinstance(page, dict):
                items.append(page)
        return items

    def all_comments(self) -> list[dict] | None:
        """Every issue comment on the PR, or None if the listing could not be read.

        Unfiltered, unlike `_summary_comments()`: the re-review guards have to
        see the starter comments and the retrigger markers too, and those are
        written by other identities.

        None is preserved rather than collapsed to `[]` for the same reason it
        is on the reviews listing — "the API did not answer" must not read as
        "nothing is there" when the emptiness is the precondition for a write.
        """
        return self._paginated(f"issues/{self.pr_number}/comments")

    def post_comment(self, body: str, token: str) -> bool:
        """Post `body` as a PR comment under `token`; True on success."""
        result = self.run(
            [
                "api",
                f"repos/{self.repo}/issues/{self.pr_number}/comments",
                "-X",
                "POST",
                "-f",
                f"body={body}",
            ],
            token=token,
        )
        if result.returncode != 0:
            print(f"::warning::could not post comment: {result.stderr.strip()}")
            return False
        return True

    def _summary_comments(self) -> list[dict]:
        """Verdict comments only: reviewer-bot-authored AND carrying a marker.

        A marker alone is not proof of authorship, so a forged verdict comment
        from any other login is filtered out here — covering both
        `newest_summary_comment_id()` and `latest_summary_body()` at once.

        An unreadable listing collapses to empty here, which is the safe
        direction for this one: no verdict found means no stamp is applied.
        """
        return [
            comment
            for comment in (self._paginated(f"issues/{self.pr_number}/comments") or [])
            if _is_verdict_comment(comment)
        ]

    def newest_summary_comment_id(self) -> int:
        """Id of the most recent SDK review summary comment, or 0 if none."""
        ids = [comment.get("id", 0) for comment in self._summary_comments()]
        return max(ids) if ids else 0

    def latest_summary_comment(self) -> dict | None:
        """The newest SDK review summary comment, or None if the PR has none.

        Ids are monotonic, so max-by-id is the newest — not merely the last in
        page order.

        Returns the whole comment rather than just its body so a caller that
        also needs the metadata (`sdk_review_reconcile.py` reads `created_at`
        to age-gate a verdict) can get both from one listing request.
        """
        comments = self._summary_comments()
        if not comments:
            return None
        return max(comments, key=lambda comment: comment.get("id", 0))

    def latest_summary_body(self) -> str:
        """Body of the newest SDK review summary comment, or "" if none."""
        newest = self.latest_summary_comment()
        return (newest or {}).get("body") or ""

    def current_labels(self) -> set[str]:
        result = self.run(
            [
                "api",
                f"repos/{self.repo}/issues/{self.pr_number}",
                "--jq",
                ".labels[].name",
            ]
        )
        if result.returncode != 0:
            print(f"::warning::could not list labels: {result.stderr.strip()}")
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def add_labels(self, labels: set[str]) -> bool:
        """Add `labels`; True on success (or nothing to do), False on failure.

        The READY path keys off this: `sdk-review-downgrade-on-ci-failure.yml`
        gates on the `sdk-review-approved` label being present, so an approval
        posted while that label is absent would never be auto-dismissed on a
        later CI failure. Reporting the failure lets the caller keep the
        label/status/approval triple from durably disagreeing.
        """
        if not labels:
            return True
        args = [
            "api",
            f"repos/{self.repo}/issues/{self.pr_number}/labels",
            "-X",
            "POST",
        ]
        for label in sorted(labels):
            args += ["-f", f"labels[]={label}"]
        result = self.run(args)
        if result.returncode != 0:
            # Surfaced, not swallowed: a token without `issues: write` fails
            # every label write while approvals still succeed.
            print(
                f"::warning::could not add labels {sorted(labels)}: "
                f"{result.stderr.strip()}"
            )
            return False
        print(f"Added labels: {sorted(labels)}")
        return True

    def remove_label(self, label: str) -> None:
        result = self.run(
            [
                "api",
                f"repos/{self.repo}/issues/{self.pr_number}/labels/{label}",
                "-X",
                "DELETE",
            ]
        )
        if result.returncode == 0:
            print(f"Removed label: {label}")
            return
        if "404" in result.stderr or "Not Found" in result.stderr:
            # Already absent — the desired end state.
            return
        print(f"::warning::could not remove label {label}: {result.stderr.strip()}")

    def post_status(self, sha: str, state: str, description: str) -> None:
        result = self.run(
            [
                "api",
                f"repos/{self.repo}/statuses/{sha}",
                "-X",
                "POST",
                "-f",
                f"state={state}",
                "-f",
                "context=sdk-review",
                "-f",
                f"description={description}",
            ]
        )
        if result.returncode != 0:
            print(
                f"::warning::could not set sdk-review status: {result.stderr.strip()}"
            )
        else:
            print(f"Set sdk-review status: {state} ({description})")

    def bot_approval_ids(self) -> list[int] | None:
        """Ids of live atlan-ci approvals bearing our signature, or None.

        None means the review listing could not be read, which every caller must
        treat as "cannot prove there is no approval" rather than "there is no
        approval". Approving on an unreadable listing is how a GitHub
        degradation turns into a pile of duplicate approvals; dismissing on one
        would be worse.
        """
        reviews = self._paginated(f"pulls/{self.pr_number}/reviews")
        if reviews is None:
            return None
        return [
            review.get("id")
            for review in reviews
            if review.get("state") == "APPROVED"
            and (review.get("user") or {}).get("login") == "atlan-ci"
            and (review.get("body") or "").startswith(APPROVAL_SIGNATURE)
        ]

    def dismiss(self, review_id: int, message: str) -> None:
        result = self.run(
            [
                "api",
                f"repos/{self.repo}/pulls/{self.pr_number}/reviews/{review_id}/dismissals",
                "-X",
                "PUT",
                "-f",
                f"message={message}",
            ]
        )
        if result.returncode != 0:
            print(
                f"::warning::could not dismiss review {review_id}: "
                f"{result.stderr.strip()}"
            )
        else:
            print(f"Dismissed stale atlan-ci approval {review_id}.")

    def rate_limit_reset(self, token: str) -> int:
        """Epoch seconds when the approver's core quota resets, 0 if unknown."""
        result = self.run(
            ["api", "rate_limit", "--jq", ".resources.core.reset"], token=token
        )
        if result.returncode != 0:
            return 0
        try:
            return int(result.stdout.strip())
        except ValueError:
            return 0

    def approve(self, sha: str, token: str) -> subprocess.CompletedProcess:
        # `gh pr review --approve` cannot pin commit_id, so use the raw API:
        # pinning the review to the sha we verified means a push landing in the
        # gap attaches the approval to the reviewed commit, not the new HEAD.
        return self.run(
            [
                "api",
                f"repos/{self.repo}/pulls/{self.pr_number}/reviews",
                "-X",
                "POST",
                "-f",
                f"commit_id={sha}",
                "-f",
                "event=APPROVE",
                "-f",
                f"body={APPROVAL_BODY}",
            ],
            token=token,
        )


def request_rereview(client: Client, stale_head: str, head_sha: str) -> str:
    """Ask for one review of `head_sha`; return the machine-readable outcome.

    Every guard fails CLOSED: an unreadable listing means we cannot prove a
    request is absent, and a duplicate `@sdk-review` costs a full sandbox run.
    A lost request is recoverable by a human tagging `@sdk-review`; a
    self-feeding trigger loop is not.

    Deliberately scoped to the stale-head refusal alone. The two neighbouring
    refusals — a verdict with no `REVIEWED_HEAD`, and an unresolvable head — are
    different faults: neither establishes a sha to review, so both keep failing
    closed rather than guessing one.

    The check-then-post below is not atomic across the two concurrency groups
    this can race with: the fast path (`sdk-review-approve-on-verdict.yml`,
    group `sdk-review-approve-<PR>`) and the slow path (`sdk-review.yml`, group
    `sdk-review-<PR>`) hold different per-PR locks, so two runs could both see
    no marker and both post `@sdk-review` for the same head. That is accepted,
    dedupe-covered behavior: the authoritative `Dedupe check` inside the
    dispatch lock re-resolves HEAD and declines the second trigger, so the
    residue is one duplicate comment plus a self-declining run — never a
    duplicate review. Closing the race outright would mean serializing every
    `stamp_verdict` caller under one per-PR group, which would make a 10–30 min
    review hold the lock while approval runs queue behind it; that cost is not
    worth eliminating a comment the dispatcher already drops.
    """
    # Posted as `atlan-ci` rather than under GH_TOKEN. Two hard requirements,
    # not a preference: `sdk-review.yml`'s job-level `if:` admits `atlan-ci`,
    # `mothership-ai[bot]` and human collaborators and nothing else, and
    # `sdk-review-dismiss-on-human.yml` excludes `atlan-ci` by name — a trigger
    # from any other identity would either be ignored by the reviewer or read as
    # human activity and dismiss the very approval the loop is chasing. It costs
    # one request against that quota, and only on this branch.
    approver_token = os.environ.get("APPROVER_TOKEN", "")
    if not approver_token:
        return "no-approver-token"

    comments = client.all_comments()
    if comments is None:
        return "comments-unreadable"
    if reviewed_at(comments, head_sha):
        return "already-reviewed"
    if retrigger_posted_for(comments, head_sha):
        return "already-requested"
    # `self_run_id=""` because this is not a review run: no starter comment on
    # the PR can be ours, so every unfinished one for this sha counts.
    if inflight_sibling_run(comments, head_sha, ""):
        return "run-in-flight"

    if not client.post_comment(retrigger_body(stale_head, head_sha), approver_token):
        return "post-failed"
    return "posted"


def post_approval_with_retry(
    client: Client,
    sha: str,
    token: str,
    max_attempts: int,
    max_wait: float,
    sleeper: Sleeper = time.sleep,
    now: Callable[[], float] = time.time,
) -> bool:
    """Approve as atlan-ci, retrying transients and short rate-limit waits.

    A secondary rate limit clears in seconds and is worth waiting out. Primary
    quota exhaustion can be up to an hour away: if the reset falls outside
    `max_wait` there is nothing useful to wait for, so fail immediately with the
    reset time named rather than burning the job timeout.
    """
    budget = max_wait
    for attempt in range(1, max_attempts + 1):
        result = client.approve(sha, token)
        if result.returncode == 0:
            return True

        stderr = result.stderr.strip()
        print(f"::warning::approval attempt {attempt}/{max_attempts} failed: {stderr}")
        if attempt == max_attempts:
            break

        delay = float(attempt * 5)
        if is_secondary_rate_limit(stderr):
            # A secondary throttle clears in seconds-to-a-minute and has no
            # relationship to the hourly core-quota reset `rate_limit_reset()`
            # reads, so wait a bounded backoff rather than that unrelated reset.
            delay = max(delay, 15.0)
        elif is_rate_limited(stderr):
            reset = client.rate_limit_reset(token)
            if reset:
                wait_for = reset - now()
                if wait_for > budget:
                    print(
                        f"::error::atlan-ci quota is exhausted and does not reset for "
                        f"{wait_for:.0f}s (> {budget:.0f}s budget) — giving up rather "
                        f"than holding the runner."
                    )
                    return False
                # +2s so we wake up after the window has actually rolled over.
                delay = max(delay, wait_for + 2)

        if delay > budget:
            print(
                f"::error::next retry needs {delay:.0f}s but only {budget:.0f}s of "
                f"wait budget remains — giving up."
            )
            return False
        budget -= delay
        print(f"Retrying approval in {delay:.0f}s ({budget:.0f}s budget left).")
        sleeper(delay)

    return False


def stamp_verdict(
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
    now: Callable[[], float] = time.time,
) -> StampOutcome:
    """Apply the verdict's labels, approval and status. See module docstring."""
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    body = os.environ.get("COMMENT_BODY", "")
    triggering_id = int(os.environ.get("TRIGGERING_COMMENT_ID") or 0)
    expected_head = os.environ.get("EXPECTED_HEAD", "").strip()
    write_status = os.environ.get("WRITE_STATUS", "true") != "false"
    require_approved_label = os.environ.get("REQUIRE_APPROVED_LABEL", "false") == "true"
    approver_token = os.environ.get("APPROVER_TOKEN", "")
    max_attempts = int(os.environ.get("APPROVE_MAX_ATTEMPTS") or 3)
    max_wait = float(os.environ.get("APPROVE_MAX_WAIT_SECONDS") or 120)

    client = Client(repo, pr_number, runner)

    # Fast path hands us the event payload; slow path asks for the newest
    # summary, which makes the freshness check below moot by construction.
    from_event = bool(body)
    if not from_event:
        body = client.latest_summary_body()
        if not body:
            print(
                f"::warning::No SDK review summary comment found on PR "
                f"#{pr_number} — skipping (the sandbox may have errored before "
                f"posting)."
            )
            return StampOutcome(SKIPPED, 0, "no summary comment")

    # Before the verdict is even read. A review-only run stamps this on its
    # comment so the A/B can review merged PRs; the verdict is a measurement,
    # not a decision, and nothing here — label, approval, status — may act on
    # it. Checked first so no later guard can be argued into approving it.
    if MARK_AB in body:
        print(
            f"PR #{pr_number}: verdict carries {MARK_AB} — a review-only run. "
            "Nothing to stamp."
        )
        return StampOutcome(SKIPPED, 0, "review-only verdict")

    verdict = extract_verdict(body)
    if not verdict:
        print(
            f"::warning::PR #{pr_number}: could not extract a verdict from the "
            f"reviewer-bot comment. Skipping."
        )
        return StampOutcome(SKIPPED, 0, "no verdict in the comment")
    print(f"Detected verdict: '{verdict}'")

    # A comment with no REVIEWED_HEAD predates the stamp; its reviewed sha
    # cannot be established, and approving blind is worse than not approving.
    reviewed_head = extract_reviewed_head(body)
    if not reviewed_head:
        print(
            f"::warning::PR #{pr_number}: verdict comment has no REVIEWED_HEAD "
            f"marker — cannot confirm which sha was reviewed; skipping all stamps."
        )
        return StampOutcome(SKIPPED, 0, "verdict has no REVIEWED_HEAD")

    head_sha = client.head_sha()
    if not head_sha:
        print(f"::warning::PR #{pr_number}: HEAD unresolvable — skipping all stamps.")
        return StampOutcome(SKIPPED, 0, "head unresolvable")

    # The verdict was computed for reviewed_head. A push since then means the
    # current head is unreviewed, and stamping it would bless unreviewed code.
    #
    # Refusing to stamp is not enough on its own, though: dropping the verdict
    # here and exiting green is how a review evaporates while the loop waits on
    # a human who was never told. So the refusal also asks for one fresh review
    # of the head that does exist — see `request_rereview`.
    if reviewed_head != head_sha:
        outcome = request_rereview(client, reviewed_head, head_sha)
        print(
            f"::warning::PR #{pr_number}: verdict was computed for {reviewed_head} "
            f"but current head is {head_sha} — skipping all stamps; "
            f"{RETRIGGER_REASONS.get(outcome, outcome)}."
        )
        return StampOutcome(SKIPPED, 0, f"head moved past the verdict ({outcome})")

    # Belt-and-suspenders for the slow path: the review can take 10–30 minutes,
    # and the sandbox could in principle have reviewed a sha other than the one
    # its run was dispatched for.
    if expected_head and head_sha != expected_head:
        print(
            f"::warning::PR #{pr_number}: head is {head_sha} but this run was "
            f"dispatched for {expected_head} — skipping all stamps."
        )
        return StampOutcome(SKIPPED, 0, "head is not the dispatched sha")

    # The job's concurrency group serialises runs per PR, but GitHub's queue is
    # not event-time ordered: after waiting, this run may execute after a newer
    # verdict has already been stamped. Ids are monotonic, so a larger one means
    # a newer summary exists and owns the final say.
    if from_event:
        newest_id = client.newest_summary_comment_id()
        if newest_id > triggering_id:
            print(
                f"::notice::PR #{pr_number}: a newer SDK_REVIEW comment "
                f"(id={newest_id}) supersedes this one (id={triggering_id}) — "
                f"skipping."
            )
            return StampOutcome(SKIPPED, 0, "superseded by a newer verdict")

    # Snapshot BEFORE reconciling: `require_approved_label` asks whether the
    # label was standing when this run started, which the post-reconcile state
    # can no longer answer.
    labels_before = client.current_labels()

    # Solo-approval guard, checked BEFORE any write. Every invalidator —
    # dismiss-on-human, downgrade-on-ci-failure, reset-on-push — clears this
    # label, so its absence under a READY verdict means something has spoken
    # since and this verdict no longer stands.
    #
    # Bailing here rather than after the label reconcile matters: `label_plan`
    # would otherwise re-add `sdk-review-approved` on the way to declining the
    # approval, resurrecting the very signal the invalidator had just stripped.
    # The PR would then wear the label with no approval behind it — and the
    # reconciler cron, which gates on exactly that label, would read the
    # resurrected label as a lost stamp and approve on the next tick.
    #
    # Only READY is gated: a NEEDS_FIXES/NEEDS_HUMAN verdict still has to clear
    # labels and dismiss a stale approval regardless of what the label says.
    if (
        verdict == READY
        and require_approved_label
        and APPROVED_LABEL not in labels_before
    ):
        print(
            f"::notice::PR #{pr_number}: `{APPROVED_LABEL}` was not present when "
            f"this run started (a downgrade, dismissal or push likely intervened) "
            f"— skipping approval, and leaving labels untouched so the label is "
            f"not resurrected."
        )
        return StampOutcome(SKIPPED, 0, "the approved label was already stripped")

    to_add, to_remove = label_plan(verdict, labels_before)
    added_ok = client.add_labels(to_add)
    for label in sorted(to_remove):
        client.remove_label(label)

    state, description = status_for(verdict)

    # The downgrade workflow gates on the `sdk-review-approved` label standing.
    # Approving + greening the status while that label never landed would leave
    # an approval no invalidator can clear on a later CI failure — so refuse to
    # stamp and fail loudly instead, leaving the status as the dispatcher set it.
    if verdict == READY and APPROVED_LABEL in to_add and not added_ok:
        print(
            f"::error::PR #{pr_number}: could not add the `{APPROVED_LABEL}` label, "
            f"so the approval is NOT being posted and the sdk-review status is left "
            f"pending. Without the label a later CI failure could not auto-dismiss "
            f"the approval. Restore `issues: write` for the App token and re-run."
        )
        return StampOutcome(FAILED, 1, "could not add the approved label")

    if verdict != READY:
        # sdk-review-dismiss-on-human.yml only fires on HUMAN activity, so when a
        # newer verdict supersedes a prior READY_TO_MERGE the stale bot approval
        # has to be cleared here or the merge gate stays open on it.
        stale = client.bot_approval_ids()
        if stale is None:
            # Cannot see the reviews, so cannot clear a superseded approval. The
            # merge gate would stay open on it, which is the wrong way to fail —
            # so leave the approval itself untouched and fail loudly to be
            # re-driven. The failure status is still written: without it a prior
            # green `sdk-review` status on this same head would outlive the
            # verdict that superseded it. The status POST is independent of the
            # reviews listing, and post_status is already best-effort (it warns
            # rather than raising), so a second degradation costs a warning, not
            # a harder failure.
            if write_status:
                client.post_status(head_sha, state, description)
            print(
                f"::error::PR #{pr_number}: verdict is {verdict} but the review "
                f"listing could not be read, so a stale bot approval cannot be "
                f"dismissed. Re-run once the API recovers."
            )
            return StampOutcome(FAILED, 1, "review listing unreadable")
        if stale:
            message = (
                f"Newer SDK review verdict ({verdict}) supersedes the prior bot "
                f"approval — auto-dismissing. Re-run `@sdk-review` when ready for "
                f"re-approval."
            )
            for review_id in stale:
                client.dismiss(review_id, message)
        if write_status:
            client.post_status(head_sha, state, description)
        print(f"Verdict '{verdict}' is not READY TO MERGE — no approval posted.")
        return StampOutcome(SKIPPED, 0, f"verdict is {verdict}")

    # Don't double-approve: sdk-review.yml's slow path runs the same stamp after
    # the SSE stream closes, and the reconciler cron runs it again every ten
    # minutes. This guard is the only thing standing between those three callers
    # and a pile of identical approvals, so it must fail CLOSED.
    #
    # It did not, until 2026-08-17: an unreadable listing came back as `[]`,
    # which reads as "no approval exists" — the precondition for posting one. A
    # GitHub degradation returning 404 on the reviews endpoint therefore had
    # `atlan-ci` re-approve the same PR on every reconciler tick.
    existing = client.bot_approval_ids()
    if existing is None:
        print(
            f"::error::PR #{pr_number}: the review listing could not be read, so "
            f"it is unknowable whether atlan-ci has already approved. Refusing to "
            f"approve blind — that is how an API outage turns into duplicate "
            f"approvals. The next run will retry."
        )
        return StampOutcome(FAILED, 1, "review listing unreadable")
    if existing:
        print(
            f"atlan-ci has already approved PR #{pr_number} with the bot "
            f"signature — skipping."
        )
        if write_status:
            client.post_status(head_sha, state, description)
        return StampOutcome(SKIPPED, 0, "already approved")

    if not approver_token:
        print("::error::APPROVER_TOKEN is not set — cannot post a codeowner approval.")
        return StampOutcome(FAILED, 1, "APPROVER_TOKEN is unset")

    approved = post_approval_with_retry(
        client,
        head_sha,
        approver_token,
        max_attempts=max_attempts,
        max_wait=max_wait,
        sleeper=sleeper,
        now=now,
    )
    if not approved:
        # Deliberately do NOT advance the commit status. Going green here without
        # an approving review is what made this failure look like success.
        print(
            f"::error::PR #{pr_number}: verdict is READY_TO_MERGE but the atlan-ci "
            f"approval could not be posted. The sdk-review status is left as-is so "
            f"the merge gate stays blocked. Re-run this workflow, or comment "
            f"`@sdk-review`, once quota has recovered."
        )
        return StampOutcome(FAILED, 1, "approval could not be posted")

    print(f"Approved PR #{pr_number} as atlan-ci (commit_id={head_sha}).")
    if write_status:
        client.post_status(head_sha, state, description)
    return StampOutcome(APPROVED, 0, f"approved at {head_sha}")


def main(
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
    now: Callable[[], float] = time.time,
) -> int:
    """Exit-code wrapper for the workflow steps. In-process callers that need to
    know WHAT happened should call `stamp_verdict()` and read its action."""
    return stamp_verdict(runner=runner, sleeper=sleeper, now=now).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
