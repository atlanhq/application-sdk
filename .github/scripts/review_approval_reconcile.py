#!/usr/bin/env python3
"""Re-drive review approvals that were computed but never posted.

Two review sources post a code-owner approval as `atlan-ci`: sdk-review
(`sdk_review_approve.py`) and lens (`lens/approve.py`). Both lose it the same
way, to one failed POST, and both are recovered here by one sweep. One sweep,
not one per source, because they spend the same two quotas: the fleet App's
reads and `atlan-ci`'s hourly REST quota, whose exhaustion is what loses the
approvals in the first place. A second cron would compete for both.

Each source is an adapter (`sdk_review_verdict`, `LensSource`) that answers,
for one PR: is there a verdict on the live head that should carry an approval,
and is that approval missing? Everything after that is shared: the dry-run
stop, the quota pre-flight, the grace and staleness rules, and reporting.

When both sources are owed an approval on one PR, each posts its own. One
approval would satisfy branch protection, but each source's invalidators only
ever dismiss that source's signed approval (`dismiss-on-human` dismisses
sdk-review's, lens's withdraw dismisses lens's). A shared approval would outlive
the verdict of whichever source withdrew. The cost is one extra `atlan-ci`
request in the rare case both approvals were lost together.

sdk-review
==========

Why this exists
---------------
`sdk-review-approve-on-verdict.yml` computes the verdict and then posts the
formal approval as `atlan-ci` — the CODEOWNER whose approval satisfies branch
protection on `main`. When that one POST fails, the run dies and nothing retries
it: the PR sits with a posted review summary, the `sdk-review-approved` label,
and no approving review. Recovery was entirely manual (`gh run rerun --failed`).

The trigger has been `atlan-ci` exhausting its 5,000 req/hr primary REST quota.
The token split in #3162 cut that path's `atlan-ci` spend to exactly one request
and added rate-limit-aware retry, which shrinks the window but cannot close it:
primary quota can reset up to an hour out, and the stamper deliberately fails
fast rather than hold a runner that long. `issue_comment` workflows also always
execute from the default branch, so that hardening can only protect PRs opened
after it lands — it could not protect its own approval.

This reconciler is the durable answer because it does not care *why* the stamp
was lost. It sweeps open PRs on a cron and re-invokes the existing stamper for
any PR whose verdict still stands but whose approval is missing.

Guards (all four must hold before a PR is touched)
--------------------------------------------------
1. `sdk-review-approved` is still on the PR. This is the solo-approval safety
   property: it is the one signal every invalidator clears — `dismiss-on-human`
   strips it (and notably does NOT touch the commit status, so the status cannot
   substitute), `downgrade-on-ci-failure` strips it, `reset-on-push` strips it.
   Reconciling without it would re-approve PRs a human has already engaged with.
2. No `atlan-ci` APPROVED review already carries the bot signature — so a
   healthy PR is a no-op and never collects a duplicate approval.
3. The newest `mothership-ai[bot]` verdict comment says READY_TO_MERGE and its
   REVIEWED_HEAD equals the PR's live head. Reconciling a verdict whose head has
   moved would bless unreviewed code.
4. That verdict comment is at least `--min-age-minutes` old, so the reconciler
   cannot race a fast-path run that is still in flight for the same comment (its
   job ceiling is 10 minutes, including rate-limit backoff). The cost is latency:
   recovery lands one grace period plus up to one cron interval after the loss,
   not within a single interval. A manual dispatch skips this guard
   (`min_age_for`): a person asking for the approval has already checked
   that the run finished.

The stamper re-checks 1, 2 and 3 itself against fresh reads, so a dismissal
landing between this sweep and the stamp is still caught. The checks here are a
prefilter — they keep the sweep cheap and tell us when a reconcile actually
happened, which is the signal worth alerting on.

What it deliberately does not do
--------------------------------
It does not write the `sdk-review` commit status (WRITE_STATUS=false). A green
status with no approving review is exactly the misleading state that made this
failure mode look like success in the first place. The approval is the thing
that was lost and the thing worth restoring; the status is left to whichever
path owns it.

lens
====

lens's last step posts its APPROVE and, when that fails, only warns: the run
stays green and the summary says ready to merge while the PR is blocked. lens
has no label for a prefilter, so `lens_ready_heads` asks one GraphQL query for
every open PR's head and the newest `lens` status on it; only a PR whose head is
green there is read further. The guards, and why a dismissed lens approval on
the head is the solo-approval guard, are on `LensSource`.

Request budget
--------------
Everything runs on the fleet App token, which carries its own quota — a
reconciler that polled every PR on the `atlan-ci` PAT would become a new source
of the exhaustion it exists to recover from. Per tick that is one paginated PR
listing and one paginated GraphQL query, plus two reads per labelled PR
(comments, then reviews) and about three per PR with a green `lens` status
(statuses, more than one page only on a busy head; comments; reviews), plus a
status re-read before each lens approval. Every `gh` call the script makes
itself is bounded by GH_TIMEOUT_SECONDS. The `atlan-ci` PAT is spent on one
request per approval posted.

Quota pre-flight
----------------
Before the first approval attempt of a run, the approver's core quota is read
via `GET /rate_limit` — free, by GitHub's own definition, and it does not count
against the quota it reports. If the quota is spent, no APPROVE is attempted at
all. The original shape discovered exhaustion by taking a 403, which spends a
doomed request to learn something a free one already knows, and repeatedly
hammering an exhausted primary limit is what escalates it into a secondary or
abuse block. The meter is read at most once per run (plus once more on a failed
stamp, to tell an exhausted-mid-run race from a real failure).

Deferral vs failure
-------------------
"Quota spent" is a DEFERRAL, not a failure: the window resets hourly and the
next tick after that posts the approval. It is annotated loudly and lands in the
job summary, but the run stays green — reddening once every ten minutes for an
hour trains everyone to ignore precisely the annotation that matters when the
problem does not clear.

A verdict that has been unapproved for longer than `--stale-after-minutes`
(default 90, above one full quota window plus a couple of intervals) has watched
a reset come and go without recovering. That is not self-healing, so it reds the
run.

In-process retry is limited to a single extra attempt inside a 45s budget, which
exists only for a SECONDARY throttle — those clear in seconds and carry their
own short window. Waiting out a PRIMARY reset in-process would hold a runner for
up to an hour, which is exactly what #3162 declined to do, and is unnecessary
here: the cron is already the long retry loop.

Exit status:
    0  swept cleanly, including approvals deferred to the next quota window
    1  a verdict is unapproved for a reason that will not fix itself — a
       non-quota failure, or quota exhaustion outliving a full reset
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# All of these need the sys.path bootstrap above.
import sdk_review_approve as approve  # noqa: E402
from lens import approve as lens_approve  # noqa: E402
from lens.findings import PRState  # noqa: E402
from lens.github import GitHub as LensGitHub  # noqa: E402
from lens.github import GitHubError  # noqa: E402
from lens.github import bot_login as lens_bot_login  # noqa: E402
from lens.review import find_state as lens_find_state  # noqa: E402

Runner = Callable[..., subprocess.CompletedProcess]

# Long enough to clear the fast path's 10-minute job ceiling (checkout, plus
# APPROVE_MAX_WAIT_SECONDS of rate-limit backoff), so a verdict this reconciler
# acts on cannot still be in flight elsewhere.
DEFAULT_MIN_AGE_MINUTES = 12

# Past this, an unapproved verdict is no longer explainable as "waiting for the
# next quota window". `atlan-ci`'s primary quota resets hourly, so a verdict that
# has outlived a full window plus a couple of cron intervals has seen at least
# one reset come and go without recovering — that is a human's problem, not a
# self-healing one, and the run goes red.
DEFAULT_STALE_AFTER_MINUTES = 90

# Ceiling on each `gh` call this script makes itself. A stalled CLI would
# otherwise hold the run until the job's own timeout, skipping the report
# and queueing every later tick behind it.
GH_TIMEOUT_SECONDS = 60

# How long after a replayed lens APPROVE the verdict is read again. Long
# enough that GitHub's read-after-write lag on reviews and statuses cannot
# hide a racing lens round from both that read and the round's own withdraw
# (see `lens.approve.approve_ready_head`). Spent only per approval posted.
LENS_CONFIRM_DELAY_SECONDS = 15.0

RECONCILED = "reconciled"
FAILED = "failed"
DEFERRED = "deferred"
SKIPPED = "skipped"

# The review sources whose `atlan-ci` approvals this sweep restores.
SDK_REVIEW = "sdk-review"
LENS = "lens"

# A dry run's reason for a PR it would have approved. The job summary lists these.
DRY_RUN_REASON = "would reconcile (dry run)"

# How each source's standing verdict is named in annotations.
VERDICT_NAMES = {
    SDK_REVIEW: "a READY_TO_MERGE verdict",
    LENS: "a lens ready-to-merge verdict",
}


@dataclass(frozen=True)
class Outcome:
    """What the sweep did about one PR for one review source, and why."""

    number: int
    action: str
    reason: str
    source: str = SDK_REVIEW


@dataclass(frozen=True)
class Quota:
    """A snapshot of the approver's primary (core) REST quota."""

    remaining: int
    reset: int

    @property
    def exhausted(self) -> bool:
        return self.remaining < 1

    def resets_in(self, now: datetime) -> int:
        return max(0, self.reset - int(now.timestamp()))


def run_gh(runner: Runner, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """`runner(argv)` under GH_TIMEOUT_SECONDS. A timeout comes back as a
    failed result (exit 124, like coreutils `timeout`), so each caller's own
    failure handling covers a stalled CLI too."""
    try:
        # setdefault, so a runner bounded twice passes one timeout, not two.
        kwargs.setdefault("timeout", GH_TIMEOUT_SECONDS)
        return runner(argv, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=argv,
            returncode=124,
            stdout="",
            stderr=f"gh timed out after {GH_TIMEOUT_SECONDS}s",
        )


def bounded(runner: Runner) -> Runner:
    """`runner` with every call under GH_TIMEOUT_SECONDS, for code that calls
    `gh` itself: the sdk-review stamper, including its APPROVE. A POST that
    times out may still have landed; the stamper's own "already approved"
    check makes the next tick a no-op in that case."""

    def call(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        return run_gh(runner, argv, **kwargs)

    return call


def list_open_prs(repo: str, runner: Runner) -> list[dict]:
    """Every open PR, with `head` and `labels` already populated.

    Those two fields are why this lists PRs rather than searching: the label
    prefilter and the head comparison both come free with the listing, so an
    unlabelled PR costs zero further requests.

    `--paginate` follows Link: rel="next", so a repo with more than 100 open
    PRs does not silently lose coverage of the rest.
    """
    result = run_gh(
        runner,
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repo}/pulls?state=open&per_page=100",
            "--jq",
            ".[] | tojson",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to list open PRs for {repo}: {result.stderr}"
        )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def approver_quota(runner: Runner, token: str) -> Quota | None:
    """The approver's core quota, or None if it cannot be read.

    `GET /rate_limit` is documented as not counting against the quota it
    reports, so this is a free pre-flight — which is the whole point. Attempting
    an APPROVE against an exhausted quota spends nothing useful (the 403 is the
    only outcome) and repeated rejected requests are what escalate a primary
    exhaustion into a secondary/abuse block. Reading first costs nothing and
    tells us whether there is any point trying.

    None means "unreadable", which callers treat as "go ahead and try": a
    failure to read the meter is not evidence the tank is empty.
    """
    if not token:
        return None
    result = run_gh(
        runner,
        ["gh", "api", "rate_limit", "--jq", ".resources.core | .remaining, .reset"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GH_TOKEN": token},
    )
    if result.returncode != 0:
        print(f"::warning::could not read the approver's rate limit: {result.stderr}")
        return None
    fields = result.stdout.split()
    if len(fields) != 2:
        return None
    try:
        return Quota(remaining=int(fields[0]), reset=int(fields[1]))
    except ValueError:
        return None


def label_names(pr: dict) -> set[str]:
    return {label.get("name", "") for label in pr.get("labels") or []}


def comment_age(comment: dict, now: datetime) -> timedelta | None:
    """How long ago `comment` was created, or None if that cannot be read.

    An unreadable timestamp means the age gate cannot be evaluated, and the
    caller treats that as "too young" — the conservative direction.
    """
    created = comment.get("created_at")
    if not created:
        return None
    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return now - created_dt


@contextmanager
def stamper_env(values: dict[str, str]) -> Iterator[None]:
    """Set `values` in os.environ for the block, then restore what was there.

    The stamper reads its inputs from the environment (it is normally a workflow
    step). Driving it per PR means rewriting those keys in a loop, so they are
    restored afterwards rather than left to leak into the next iteration.
    """
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def stamp(
    repo: str,
    pr_number: int,
    head_sha: str,
    runner: Runner,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> approve.StampOutcome:
    """Invoke the stamper for one PR and return what it actually did.

    `stamp_verdict` reports its own action rather than the caller inferring one
    from the exit code, which cannot separate "approved" from "a guard
    declined". Inferring it from a follow-up read of the reviews listing does
    not work either — that listing is read-after-write eventually consistent,
    and the first live run of this cron approved PR #3232, re-read, saw nothing,
    and reported a decline.


    The environment matches the slow path (`sdk-review.yml`) — no event payload,
    no commit-status write, label guard on — with only a short retry budget,
    because this cron is itself the retry loop for anything longer.
    """
    env = {
        "REPO": repo,
        "PR_NUMBER": str(pr_number),
        # Empty: re-read the newest summary comment off the PR rather than an
        # event payload. There is no event here.
        "COMMENT_BODY": "",
        "TRIGGERING_COMMENT_ID": "",
        # Staleness guard, re-evaluated against a fresh read inside the stamper.
        "EXPECTED_HEAD": head_sha,
        # A green `sdk-review` status with no approving review is the state this
        # whole mechanism exists to avoid creating.
        "WRITE_STATUS": "false",
        # Solo-approval guard: refuse if `sdk-review-approved` has gone in the
        # gap between this sweep's listing and the stamp.
        "REQUIRE_APPROVED_LABEL": "true",
        # One `atlan-ci` request on the success path. The second attempt exists
        # only for a SECONDARY throttle, which clears in seconds and is worth
        # waiting out inline; the quota pre-flight above already catches primary
        # exhaustion, and the stamper bails immediately on a reset it cannot
        # reach inside this budget. Anything longer than 45s is the next tick's
        # job, not this runner's.
        "APPROVE_MAX_ATTEMPTS": "2",
        "APPROVE_MAX_WAIT_SECONDS": "45",
    }
    with stamper_env(env):
        # Bounded here, where the stamper gets it, whatever the caller passed:
        # it calls `gh` itself, the APPROVE included.
        return approve.stamp_verdict(runner=bounded(runner), sleeper=sleeper, now=clock)


def _blocked_outcome(
    number: int,
    blocker: str,
    age: timedelta,
    stale_after: timedelta,
    source: str = SDK_REVIEW,
) -> Outcome:
    """Classify a PR that is owed an approval we cannot currently post.

    Deferring is the normal case and must not go red. Both blockers this covers
    — a spent quota, an unreadable review listing — are transient by nature, and
    a red run per tick while one clears would bury the signal it is supposed to
    raise.

    But neither is transient forever. A verdict still unapproved past
    `stale_after` has outlived a full quota window, which means a reset came and
    went without recovery, or an API degradation has outlasted any reasonable
    blip. That does need a human, so it reds the run. Without this the reconciler
    could sit in a permanently green "skipped" loop through an outage, saying
    nothing — the same silence the whole workflow exists to break.
    """
    if age > stale_after:
        return Outcome(
            number,
            FAILED,
            f"unapproved for {age.total_seconds() / 60:.0f}min — {blocker}, and "
            f"that has now outlasted a full quota window",
            source,
        )
    return Outcome(number, DEFERRED, blocker, source)


@dataclass(frozen=True)
class Owed:
    """A PR one review source says is owed an approval that is not there.

    `post` makes the one `atlan-ci` request and says what came of it: RECONCILED,
    SKIPPED (a guard declined, with its reason) or FAILED.
    """

    number: int
    source: str
    age: timedelta
    post: Callable[[], tuple[str, str]]


Verdict = Outcome | Owed | None


def sdk_review_verdict(
    repo: str,
    pr: dict,
    *,
    runner: Runner,
    min_age: timedelta,
    stale_after: timedelta,
    now: datetime,
    sleeper: Callable[[float], None],
) -> Verdict:
    """sdk-review's guards 1-4 for one PR (see the module docstring).

    None when the PR is not sdk-review's (no `sdk-review-approved` label): that
    is almost every PR, and it costs nothing because the listing carries labels.

    The comment listing is read before the review listing, deliberately, even
    though reviews would short-circuit more PRs. The review check needs the
    verdict's age to decide whether an unreadable listing is a blip to defer or
    an outage to escalate, and the age comes from the comment. One extra
    App-token read per labelled PR buys an escalation path that would otherwise
    not exist.
    """
    number = pr["number"]
    if approve.APPROVED_LABEL not in label_names(pr):
        return None

    client = approve.Client(repo, str(number), bounded(runner))

    comment = client.latest_summary_comment()
    if comment is None:
        return Outcome(number, SKIPPED, "no verdict comment")

    body = comment.get("body") or ""
    verdict = approve.extract_verdict(body)
    if verdict != approve.READY:
        return Outcome(number, SKIPPED, f"verdict is {verdict}")

    reviewed_head = approve.extract_reviewed_head(body)
    head_sha = ((pr.get("head") or {}).get("sha") or "").strip()
    if not reviewed_head or not head_sha or reviewed_head != head_sha:
        return Outcome(
            number,
            SKIPPED,
            f"head moved past the verdict ({reviewed_head} -> {head_sha})",
        )

    age = comment_age(comment, now)
    if age is None or age < min_age:
        return Outcome(number, SKIPPED, "verdict too recent to be lost")

    # None is not []: an unreadable listing cannot prove there is no approval,
    # and treating it as proof is what turned a GitHub degradation into
    # duplicate approvals on every tick. Checked after `age` so a listing that
    # stays broken can escalate rather than skip forever.
    approvals = client.bot_approval_ids()
    if approvals is None:
        return _blocked_outcome(
            number,
            "the review listing is unreadable, so it is unknowable "
            "whether an approval already exists",
            age,
            stale_after,
        )
    if approvals:
        return Outcome(number, SKIPPED, "already approved")

    def post() -> tuple[str, str]:
        stamped = stamp(
            repo, number, head_sha, runner, sleeper=sleeper, clock=now.timestamp
        )
        if stamped.action == approve.APPROVED:
            return RECONCILED, stamped.detail
        if stamped.action == approve.SKIPPED:
            # The stamper re-reads the label, head and comments, so a dismissal
            # landing between this sweep's listing and the stamp is caught
            # there. It says so itself rather than us inferring it.
            return SKIPPED, f"the stamper declined — {stamped.detail}"
        return FAILED, stamped.detail

    return Owed(number, SDK_REVIEW, age, post)


# One GraphQL query for every open PR's head and the newest `lens` status on it.
# This is lens's free prefilter, the counterpart of sdk-review's label: lens
# adds no label, so without it every open PR would cost a comment read per tick.
LENS_READY_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        headRefOid
        commits(last: 1) {
          nodes { commit { oid status { context(name: "lens") { state createdAt } } } }
        }
      }
    }
  }
}
"""


def lens_ready_heads(repo: str, runner: Runner) -> dict[tuple[int, str], str]:
    """When lens's status went green, keyed by (PR number, head SHA), for every
    open PR whose head's `lens` status is green.

    The timestamp gives the PR an age even when a later REST read fails, so an
    unreadable status or summary defers and can escalate like any other
    blocked approval, instead of skipping quietly on every tick.

    Only a prefilter. Who set the status, and everything else, is re-read over
    REST for the few PRs it lets through. A failure reds the run, as a failed PR
    listing does: it is the same kind of read, and a lens sweep that silently
    saw nothing would be the silence this workflow exists to break.
    """
    owner, name = repo.split("/", 1)
    result = run_gh(
        runner,
        [
            "gh",
            "api",
            "graphql",
            "--paginate",
            "-f",
            f"query={LENS_READY_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "--jq",
            ".data.repository.pullRequests.nodes[] | tojson",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::failed to list lens verdicts for {repo}: {result.stderr}"
        )
    ready: dict[tuple[int, str], str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        node = json.loads(line)
        head = node.get("headRefOid") or ""
        for commit_node in (node.get("commits") or {}).get("nodes") or []:
            commit = commit_node.get("commit") or {}
            context = (commit.get("status") or {}).get("context") or {}
            if head and commit.get("oid") == head and context.get("state") == "SUCCESS":
                ready[(int(node["number"]), head)] = context.get("createdAt") or ""
    return ready


def lens_not_ready(state: PRState) -> str:
    """Why lens's recorded state for its reviewed head carries no approval, or
    "" when it does.

    The conditions in `lens.approve.decision_for`, read back from the state
    marker instead of a live run: files left pending, any open finding at any
    level, and a latest round recorded as incomplete.
    """
    if state.pending_files:
        return f"{len(state.pending_files)} file(s) still pending review"
    open_findings = state.open_findings()
    if open_findings:
        return f"{len(open_findings)} open finding(s)"
    if state.history and state.history[-1].get("incomplete"):
        return "the latest round was incomplete"
    return ""


class LensSource:
    """lens's guards for one PR. All must hold before the PR is owed anything.

    1. The newest `lens` commit status on the live head is `success` and was
       set by lens's own App. That status is lens's last word on the head: it
       is `pending` while a round runs (so an in-flight round is never raced),
       `error` when a round failed or never started (the cases in which lens
       withdraws without changing its state marker), and `failure` while
       anything is open. Anyone with statuses:write can set a `lens` status, so
       one from another creator is never trusted, only ever a reason to skip.
    2. lens's sticky summary decodes, its `reviewed_head` is the live head, and
       the state says ready (`lens_not_ready`).
    3. The status is at least `min_age` old, so the run that set it has had
       time to reach its approval step.
    4. No lens-signed `atlan-ci` review on this head is APPROVED (a healthy PR
       is a no-op) or DISMISSED. This is the solo-approval guard. sdk-review
       has a label every invalidator clears; lens has none, so this keys on
       what an invalidation leaves on lens's own approval. A dismissed lens
       approval on the head means lens withdrew it (a later round on that head
       was not ready) or a person dismissed it, and only a new lens verdict may
       approve that head again. A push moves the head, which guards 1 and 2
       already catch; the ruleset's dismiss-on-push only affects approvals on
       older heads.

    `lens.approve.approve_ready_head` then re-checks against fresh reads (open,
    not a draft, head unchanged, not self-approval, no approval and no
    withdrawal on the head), reads guard 1 again last, and posts. A `/lens`
    round that ends not ready on this head before the approval exists leaves
    nothing to withdraw, only its status, so guard 1 is read once more
    LENS_CONFIRM_DELAY_SECONDS after the POST, and the approval is dismissed
    if the verdict changed meanwhile.

    A status or summary that cannot be read is a blocked approval, aged from
    the prefilter's timestamp: deferred while young, red once it outlives a
    quota window. It is never a quiet skip.

    Human activity on its own does not stop a lens approval, here or in lens:
    a posted lens approval survives a human comment, because `dismiss-on-human`
    only dismisses sdk-review's signature. The reconciler restores what lens's
    last step would have left, no more.
    """

    def __init__(
        self,
        gh: LensGitHub,
        approver: LensGitHub,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """`gh` (fleet App token) does every read; `approver` (the `atlan-ci`
        PAT) is used for the APPROVE call only, as in lens's own last step."""
        self.gh = gh
        self.approver = approver
        self.sleeper = sleeper

    @classmethod
    def from_env(cls, repo: str) -> LensSource:
        return cls(
            LensGitHub(repo, token=os.environ.get("GH_TOKEN", "")),
            LensGitHub(repo, token=os.environ.get("APPROVER_TOKEN", "")),
        )

    def green_status(self, head: str) -> dict | str:
        """lens's newest `lens` status on `head` when it is lens's own green,
        else why not. Raises GitHubError when the statuses are unreadable."""
        status = self.gh.newest_status(head, "lens")
        if status is None:
            return "no lens status on the head"
        creator = (status.get("creator") or {}).get("login")
        if creator != lens_bot_login():
            return f"the newest lens status was set by {creator}, not lens"
        if status.get("state") != "success":
            return f"the lens status is {status.get('state')}"
        return status

    def verdict(
        self,
        pr: dict,
        ready: dict[tuple[int, str], str],
        *,
        min_age: timedelta,
        stale_after: timedelta,
        now: datetime,
    ) -> Verdict:
        """None when the prefilter saw no green `lens` status on this head."""
        number = pr["number"]
        head = ((pr.get("head") or {}).get("sha") or "").strip()
        if not head or (number, head) not in ready:
            return None

        def skip(reason: str) -> Outcome:
            return Outcome(number, SKIPPED, reason, LENS)

        def unreadable(what: str) -> Outcome:
            # The prefilter's timestamp stands in for the status's own: a read
            # that keeps failing must reach the stale path, not skip forever.
            age = comment_age({"created_at": ready[(number, head)]}, now)
            if age is None or age < min_age:
                return skip("verdict too recent to be lost")
            return _blocked_outcome(
                number,
                f"{what} is unreadable, so it is unknowable whether an "
                f"approval is owed",
                age,
                stale_after,
                LENS,
            )

        try:
            status = self.green_status(head)
        except GitHubError:
            return unreadable("the lens status")
        if isinstance(status, str):
            return skip(status)

        try:
            state, _ = lens_find_state(self.gh, number)
        except GitHubError:
            return unreadable("the lens summary")
        if state is None:
            return skip("no lens summary with a readable state")
        if state.reviewed_head != head:
            return skip(
                f"head moved past the verdict ({state.reviewed_head} -> {head})"
            )
        not_ready = lens_not_ready(state)
        if not_ready:
            return skip(f"lens is not ready: {not_ready}")

        age = comment_age(status, now)
        if age is None or age < min_age:
            return skip("verdict too recent to be lost")

        try:
            reviews = self.gh.reviews(number)
        except GitHubError:
            return _blocked_outcome(
                number,
                "the review listing is unreadable, so it is unknowable "
                "whether an approval already exists",
                age,
                stale_after,
                LENS,
            )
        on_head = [
            r
            for r in reviews
            if (r.get("user") or {}).get("login") == lens_approve.APPROVER_LOGIN
            and r.get("commit_id") == head
            and (r.get("body") or "").startswith(lens_approve.SIGNATURE)
        ]
        if any(r.get("state") == "APPROVED" for r in on_head):
            return skip("already approved")
        if any(r.get("state") == "DISMISSED" for r in on_head):
            return skip(
                "a lens approval on this head was withdrawn; only a new lens "
                "round may approve it again"
            )

        decision = {
            "action": "approve",
            "pr": number,
            "head": head,
            "round": state.round,
        }

        def still_ready() -> str:
            # lens's verdict, re-read by the approve step twice: last before the
            # POST, and again after it (see `approve_ready_head`). A `/lens`
            # round that ends not ready on this head leaves no approval to
            # withdraw if it ends before ours exists; its status is what shows
            # it. A round still running shows `pending`.
            try:
                current = self.green_status(head)
            except GitHubError as exc:
                raise lens_approve.VerdictUnreadable(
                    f"the lens status is unreadable: {exc}"
                ) from exc
            if isinstance(current, str):
                return f"the lens verdict changed before approval: {current}"
            return ""

        def post() -> tuple[str, str]:
            try:
                approval = lens_approve.approve_ready_head(
                    self.gh,
                    self.approver,
                    decision,
                    refuse_after_withdrawal=True,
                    still_ready=still_ready,
                    confirm_delay=LENS_CONFIRM_DELAY_SECONDS,
                    sleeper=self.sleeper,
                )
            except lens_approve.VerdictUnreadable as exc:
                # Only the pre-POST read raises: nothing was posted.
                return DEFERRED, str(exc)
            except GitHubError as exc:
                return FAILED, f"the approval step failed: {exc}"
            if approval.posted:
                return RECONCILED, approval.detail
            return SKIPPED, f"lens declined — {approval.detail}"

        return Owed(number, LENS, age, post)


def sweep(
    repo: str,
    *,
    runner: Runner = subprocess.run,
    min_age: timedelta = timedelta(minutes=DEFAULT_MIN_AGE_MINUTES),
    stale_after: timedelta = timedelta(minutes=DEFAULT_STALE_AFTER_MINUTES),
    now: datetime | None = None,
    dry_run: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
    lens: LensSource | None = None,
    only_pr: int | None = None,
) -> list[Outcome]:
    """Reconcile every open PR whose standing verdict lost its approval.

    One PR listing, then each source is asked about each PR. Whatever a source
    says is owed goes through one shared settle step: the dry-run stop, the
    approver quota (read at most once per run, and only once a PR has actually
    earned an attempt), the one `atlan-ci` request, and the classification of
    anything that did not land.
    """
    now = now or datetime.now(timezone.utc)
    approver_token = os.environ.get("APPROVER_TOKEN", "")
    outcomes: list[Outcome] = []
    quota_checked = False
    quota: Quota | None = None

    prs = list_open_prs(repo, runner)
    lens_ready = lens_ready_heads(repo, runner)
    lens_source = lens or LensSource.from_env(repo)

    for pr in prs:
        number = pr.get("number")
        if number is None or (only_pr is not None and number != only_pr):
            continue

        verdicts: list[Verdict] = [
            sdk_review_verdict(
                repo,
                pr,
                runner=runner,
                min_age=min_age,
                stale_after=stale_after,
                now=now,
                sleeper=sleeper,
            ),
            lens_source.verdict(
                pr, lens_ready, min_age=min_age, stale_after=stale_after, now=now
            ),
        ]
        for verdict in verdicts:
            if verdict is None:
                continue
            if isinstance(verdict, Outcome):
                outcomes.append(verdict)
                continue

            owed, source = verdict, verdict.source
            if dry_run:
                outcomes.append(Outcome(number, SKIPPED, DRY_RUN_REASON, source))
                continue

            # Read the meter once per run, and only now — a sweep that finds
            # nothing to approve should not spend a request establishing that
            # it could have. One reading covers every source: they all spend
            # the same `atlan-ci` quota.
            if not quota_checked:
                quota, quota_checked = approver_quota(runner, approver_token), True
            if quota is not None and quota.exhausted:
                outcomes.append(
                    _blocked_outcome(
                        number,
                        f"atlan-ci quota exhausted; resets in "
                        f"{quota.resets_in(now) // 60}min",
                        owed.age,
                        stale_after,
                        source,
                    )
                )
                continue

            action, detail = owed.post()
            if action != FAILED:
                outcomes.append(Outcome(number, action, detail, source))
                continue
            # Re-read the meter rather than parsing the failure text: the quota
            # can empty between the pre-flight and the POST (other `atlan-ci`
            # workflows share it), and that race is a deferral, not a failure.
            # Free, and only on a path that has already failed.
            quota = approver_quota(runner, approver_token)
            if quota is not None and quota.exhausted:
                outcomes.append(
                    _blocked_outcome(
                        number,
                        f"atlan-ci quota emptied mid-run; resets in "
                        f"{quota.resets_in(now) // 60}min",
                        owed.age,
                        stale_after,
                        source,
                    )
                )
            else:
                outcomes.append(Outcome(number, FAILED, detail, source))

    return outcomes


def report(outcomes: list[Outcome], repo: str) -> None:
    """Log the sweep, and annotate anything that was not a plain no-op.

    Reconciling is not routine — it means a stamp was lost upstream — so it is
    a ::warning:: rather than a ::notice::, and it lands in the job summary too.
    Silent recovery would hide a worsening rate-limit problem, which is the
    thing most worth knowing about here.

    A deferral is the one case that is loud but not red. It says the approval is
    owed and the quota is gone, which the next tick after the reset will fix on
    its own; reddening the run once per tick for an hour would train everyone to
    ignore exactly the annotation that matters when it does not fix itself.
    """
    for outcome in outcomes:
        print(
            f"PR #{outcome.number} [{outcome.source}]: {outcome.action} — "
            f"{outcome.reason}"
        )

    reconciled = [o for o in outcomes if o.action == RECONCILED]
    deferred = [o for o in outcomes if o.action == DEFERRED]
    failed = [o for o in outcomes if o.action == FAILED]
    would = [o for o in outcomes if o.reason == DRY_RUN_REASON]

    for outcome in reconciled:
        print(
            f"::warning::PR #{outcome.number}: {VERDICT_NAMES[outcome.source]} had "
            f"lost its atlan-ci approval; the reconciler posted it "
            f"({outcome.reason}). The {outcome.source} step that should have "
            f"posted it failed — check that run."
        )
    for outcome in deferred:
        print(
            f"::warning::PR #{outcome.number}: {VERDICT_NAMES[outcome.source]} is "
            f"owed an atlan-ci approval, but {outcome.reason}. No approval was "
            f"attempted; a later run will post it once that clears."
        )
    for outcome in failed:
        print(
            f"::error::PR #{outcome.number}: {VERDICT_NAMES[outcome.source]} is "
            f"still missing its atlan-ci approval and the reconciler could not "
            f"post it either — {outcome.reason}."
        )

    if not (reconciled or deferred or failed or would):
        print("Nothing to reconcile.")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path or not (reconciled or deferred or failed or would):
        return
    lines = ["## Review approvals reconciled", ""]
    for outcome in reconciled + deferred + failed + would:
        url = f"https://github.com/{repo}/pull/{outcome.number}"
        lines.append(
            f"- **{outcome.action}** [#{outcome.number}]({url}) "
            f"({outcome.source}) — {outcome.reason}"
        )
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def min_age_for(event_name: str, min_age_minutes: int) -> timedelta:
    """The grace a verdict must outlive before its missing approval counts as lost.

    The grace exists only so the cron does not race the source's own approval
    run while that run may still be retrying. Racing it is not unsafe (every
    approval path re-reads the verdict, head and label or status right before
    posting), it just risks a duplicate approval. A manual dispatch is a person
    who has looked at the PR, seen the approval missing and the run finished,
    and asked for it now. Making them wait out a timer built for the unattended
    case, and reporting "too recent" instead, is the failure this removes.
    """
    if event_name == "workflow_dispatch":
        return timedelta(0)
    return timedelta(minutes=min_age_minutes)


def parse_pr(value: str) -> int | None:
    """`--pr` as a PR number, or None for "every PR". Empty is what the
    workflow passes on a cron tick, where there is no `pr` input."""
    value = value.strip()
    if not value:
        return None
    try:
        number = int(value.lstrip("#"))
    except ValueError:
        raise SystemExit(f"::error::--pr must be a PR number, got {value!r}")
    if number < 1:
        raise SystemExit(f"::error::--pr must be a PR number, got {value!r}")
    return number


def main(argv: list[str] | None = None, runner: Runner = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", required=True, help="owner/repo, e.g. atlanhq/application-sdk"
    )
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=DEFAULT_MIN_AGE_MINUTES,
        help=(
            "Age a verdict comment must reach before its missing approval counts "
            "as lost rather than in flight (default "
            f"{DEFAULT_MIN_AGE_MINUTES}min, above the fast path's 10min ceiling)."
        ),
    )
    parser.add_argument(
        "--stale-after-minutes",
        type=int,
        default=DEFAULT_STALE_AFTER_MINUTES,
        help=(
            "Age past which an unapproved verdict blocked on quota stops counting "
            "as self-healing and reds the run (default "
            f"{DEFAULT_STALE_AFTER_MINUTES}min, above one hourly quota window)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which PRs would be reconciled without approving any of them.",
    )
    parser.add_argument(
        "--event-name",
        default="schedule",
        help=(
            "The GitHub event that started this run. `workflow_dispatch` means a "
            "person asked for it, and the min-age grace does not apply."
        ),
    )
    parser.add_argument(
        "--pr",
        default="",
        help="Reconcile only this PR number. Empty (the default) sweeps every PR.",
    )
    args = parser.parse_args(argv)

    outcomes = sweep(
        args.repo,
        runner=runner,
        min_age=min_age_for(args.event_name, args.min_age_minutes),
        stale_after=timedelta(minutes=args.stale_after_minutes),
        dry_run=args.dry_run,
        only_pr=parse_pr(args.pr),
    )
    report(outcomes, args.repo)
    return 1 if any(outcome.action == FAILED for outcome in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
