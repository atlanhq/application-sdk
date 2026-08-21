#!/usr/bin/env python3
"""Re-drive SDK review approvals that were computed but never posted.

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
   not within a single interval.

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

Request budget
--------------
Everything runs on the fleet App token, which carries its own quota — a
reconciler that polled every PR on the `atlan-ci` PAT would become a new source
of the exhaustion it exists to recover from. Per tick that is one paginated PR
listing, plus two reads per labelled PR (reviews, then comments). The
`atlan-ci` PAT is spent on one request per PR approved.

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

import sdk_review_approve as approve  # noqa: E402  (needs the sys.path bootstrap)

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

RECONCILED = "reconciled"
FAILED = "failed"
DEFERRED = "deferred"
SKIPPED = "skipped"


@dataclass(frozen=True)
class Outcome:
    """What the sweep did about one PR, and why."""

    number: int
    action: str
    reason: str


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


def list_open_prs(repo: str, runner: Runner) -> list[dict]:
    """Every open PR, with `head` and `labels` already populated.

    Those two fields are why this lists PRs rather than searching: the label
    prefilter and the head comparison both come free with the listing, so an
    unlabelled PR costs zero further requests.

    `--paginate` follows Link: rel="next", so a repo with more than 100 open
    PRs does not silently lose coverage of the rest.
    """
    result = runner(
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
    result = runner(
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
        return approve.stamp_verdict(runner=runner, sleeper=sleeper, now=clock)


def _blocked_outcome(
    number: int, blocker: str, age: timedelta, stale_after: timedelta
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
        )
    return Outcome(number, DEFERRED, blocker)


def sweep(
    repo: str,
    *,
    runner: Runner = subprocess.run,
    min_age: timedelta = timedelta(minutes=DEFAULT_MIN_AGE_MINUTES),
    stale_after: timedelta = timedelta(minutes=DEFAULT_STALE_AFTER_MINUTES),
    now: datetime | None = None,
    dry_run: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[Outcome]:
    """Reconcile every open PR whose standing verdict lost its approval.

    The label check is free (the PR listing carries labels) and screens out
    almost everything. The comment listing comes next, then the review listing —
    deliberately in that order, even though reviews would short-circuit more
    PRs. The review check needs the verdict's age to decide whether an
    unreadable listing is a blip to defer or an outage to escalate, and the age
    comes from the comment. One extra App-token read per labelled PR buys an
    escalation path that would otherwise not exist. The approver's quota is read
    at most once per run, and only once a PR has actually earned an attempt.
    """
    now = now or datetime.now(timezone.utc)
    approver_token = os.environ.get("APPROVER_TOKEN", "")
    outcomes: list[Outcome] = []
    quota_checked = False
    quota: Quota | None = None

    for pr in list_open_prs(repo, runner):
        number = pr.get("number")
        if number is None:
            continue

        if approve.APPROVED_LABEL not in label_names(pr):
            continue

        client = approve.Client(repo, str(number), runner)

        comment = client.latest_summary_comment()
        if comment is None:
            outcomes.append(Outcome(number, SKIPPED, "no verdict comment"))
            continue

        body = comment.get("body") or ""
        verdict = approve.extract_verdict(body)
        if verdict != approve.READY:
            outcomes.append(Outcome(number, SKIPPED, f"verdict is {verdict}"))
            continue

        reviewed_head = approve.extract_reviewed_head(body)
        head_sha = ((pr.get("head") or {}).get("sha") or "").strip()
        if not reviewed_head or not head_sha or reviewed_head != head_sha:
            outcomes.append(
                Outcome(
                    number,
                    SKIPPED,
                    f"head moved past the verdict ({reviewed_head} -> {head_sha})",
                )
            )
            continue

        age = comment_age(comment, now)
        if age is None or age < min_age:
            outcomes.append(Outcome(number, SKIPPED, "verdict too recent to be lost"))
            continue

        # None is not []: an unreadable listing cannot prove there is no
        # approval, and treating it as proof is what turned a GitHub degradation
        # into duplicate approvals on every tick. Checked after `age` so a
        # listing that stays broken can escalate rather than skip forever.
        approvals = client.bot_approval_ids()
        if approvals is None:
            outcomes.append(
                _blocked_outcome(
                    number,
                    "the review listing is unreadable, so it is unknowable "
                    "whether an approval already exists",
                    age,
                    stale_after,
                )
            )
            continue
        if approvals:
            outcomes.append(Outcome(number, SKIPPED, "already approved"))
            continue

        if dry_run:
            outcomes.append(Outcome(number, SKIPPED, "would reconcile (dry run)"))
            continue

        # Read the meter once per run, and only now — a sweep that finds nothing
        # to approve should not spend a request establishing that it could have.
        if not quota_checked:
            quota, quota_checked = approver_quota(runner, approver_token), True
        if quota is not None and quota.exhausted:
            outcomes.append(
                _blocked_outcome(
                    number,
                    f"atlan-ci quota exhausted; resets in "
                    f"{quota.resets_in(now) // 60}min",
                    age,
                    stale_after,
                )
            )
            continue

        stamped = stamp(
            repo,
            number,
            head_sha,
            runner,
            sleeper=sleeper,
            clock=now.timestamp,
        )
        if stamped.action == approve.APPROVED:
            outcomes.append(Outcome(number, RECONCILED, stamped.detail))
        elif stamped.action == approve.SKIPPED:
            # The stamper re-reads the label, head and comments, so a dismissal
            # landing between this sweep's listing and the stamp is caught
            # there. It says so itself rather than us inferring it.
            outcomes.append(
                Outcome(number, SKIPPED, f"the stamper declined — {stamped.detail}")
            )
        else:
            # Re-read the meter rather than parsing the stamper's stderr: the
            # quota can empty between the pre-flight and the POST (other
            # `atlan-ci` workflows share it), and that race is a deferral, not a
            # failure. Free, and only on a path that has already failed.
            quota = approver_quota(runner, approver_token)
            if quota is not None and quota.exhausted:
                outcomes.append(
                    _blocked_outcome(
                        number,
                        f"atlan-ci quota emptied mid-run; resets in "
                        f"{quota.resets_in(now) // 60}min",
                        age,
                        stale_after,
                    )
                )
            else:
                outcomes.append(Outcome(number, FAILED, stamped.detail))

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
        print(f"PR #{outcome.number}: {outcome.action} — {outcome.reason}")

    reconciled = [o for o in outcomes if o.action == RECONCILED]
    deferred = [o for o in outcomes if o.action == DEFERRED]
    failed = [o for o in outcomes if o.action == FAILED]

    for outcome in reconciled:
        print(
            f"::warning::PR #{outcome.number}: a READY_TO_MERGE verdict had lost "
            f"its atlan-ci approval; the reconciler posted it ({outcome.reason}). "
            f"The stamper that should have posted it failed — check that run."
        )
    for outcome in deferred:
        print(
            f"::warning::PR #{outcome.number}: a READY_TO_MERGE verdict is owed an "
            f"atlan-ci approval, but {outcome.reason}. No approval was attempted; "
            f"a later run will post it once that clears."
        )
    for outcome in failed:
        print(
            f"::error::PR #{outcome.number}: a READY_TO_MERGE verdict is still "
            f"missing its atlan-ci approval and the reconciler could not post it "
            f"either — {outcome.reason}."
        )

    if not (reconciled or deferred or failed):
        print("Nothing to reconcile.")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path or not (reconciled or deferred or failed):
        return
    lines = ["## SDK review approvals reconciled", ""]
    for outcome in reconciled + deferred + failed:
        url = f"https://github.com/{repo}/pull/{outcome.number}"
        lines.append(
            f"- **{outcome.action}** [#{outcome.number}]({url}) — {outcome.reason}"
        )
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


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
    args = parser.parse_args(argv)

    outcomes = sweep(
        args.repo,
        runner=runner,
        min_age=timedelta(minutes=args.min_age_minutes),
        stale_after=timedelta(minutes=args.stale_after_minutes),
        dry_run=args.dry_run,
    )
    report(outcomes, args.repo)
    return 1 if any(outcome.action == FAILED for outcome in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
