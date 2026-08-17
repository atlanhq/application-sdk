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
`atlan-ci` PAT is spent only inside the stamper's APPROVE call, and
APPROVE_MAX_ATTEMPTS=1 holds that to exactly one request per PR approved: this
cron *is* the retry loop, so retrying in-process would only duplicate it.

Exit status:
    0  swept cleanly (whether or not anything needed reconciling)
    1  at least one PR needed reconciling and the approval still could not be
       posted — the run goes red so a worsening quota problem stays visible
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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

RECONCILED = "reconciled"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class Outcome:
    """What the sweep did about one PR, and why."""

    number: int
    action: str
    reason: str


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


def stamp(repo: str, pr_number: int, head_sha: str, runner: Runner) -> bool:
    """Invoke the stamper for one PR. True when the approval landed.

    The environment matches the slow path (`sdk-review.yml`) — no event payload,
    no commit-status write, label guard on — with retries disabled, because this
    cron is itself the retry loop.
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
        # Exactly one `atlan-ci` request per PR approved. Waiting out a primary
        # quota reset in-process is what the stamper already declines to do; the
        # next tick is the retry.
        "APPROVE_MAX_ATTEMPTS": "1",
        "APPROVE_MAX_WAIT_SECONDS": "0",
    }
    with stamper_env(env):
        return approve.main(runner=runner) == 0


def sweep(
    repo: str,
    *,
    runner: Runner = subprocess.run,
    min_age: timedelta = timedelta(minutes=DEFAULT_MIN_AGE_MINUTES),
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[Outcome]:
    """Reconcile every open PR whose standing verdict lost its approval.

    Ordering is by cost: the label check is free (the PR listing carries
    labels), the review listing short-circuits the healthy majority, and only
    then is the comment listing spent.
    """
    now = now or datetime.now(timezone.utc)
    outcomes: list[Outcome] = []

    for pr in list_open_prs(repo, runner):
        number = pr.get("number")
        if number is None:
            continue

        if approve.APPROVED_LABEL not in label_names(pr):
            continue

        client = approve.Client(repo, str(number), runner)

        if client.bot_approval_ids():
            outcomes.append(Outcome(number, SKIPPED, "already approved"))
            continue

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

        if dry_run:
            outcomes.append(Outcome(number, SKIPPED, "would reconcile (dry run)"))
            continue

        if not stamp(repo, number, head_sha, runner):
            outcomes.append(Outcome(number, FAILED, "approval could not be posted"))
        elif client.bot_approval_ids():
            outcomes.append(Outcome(number, RECONCILED, f"approved at {head_sha}"))
        else:
            # The stamper exits 0 both when it approved and when one of its own
            # guards declined — it re-reads the label, head and comments, so a
            # dismissal landing between this sweep's listing and the stamp is
            # caught there. Confirming the review exists is what separates the
            # two, and the whole point of this cron is not to report a recovery
            # that did not happen.
            outcomes.append(
                Outcome(number, SKIPPED, "the stamper declined — verdict invalidated")
            )

    return outcomes


def report(outcomes: list[Outcome], repo: str) -> None:
    """Log the sweep, and annotate anything that was not a plain no-op.

    Reconciling is not routine — it means a stamp was lost upstream — so it is
    a ::warning:: rather than a ::notice::, and it lands in the job summary too.
    Silent recovery would hide a worsening rate-limit problem, which is the
    thing most worth knowing about here.
    """
    for outcome in outcomes:
        print(f"PR #{outcome.number}: {outcome.action} — {outcome.reason}")

    reconciled = [o for o in outcomes if o.action == RECONCILED]
    failed = [o for o in outcomes if o.action == FAILED]

    for outcome in reconciled:
        print(
            f"::warning::PR #{outcome.number}: a READY_TO_MERGE verdict had lost "
            f"its atlan-ci approval; the reconciler posted it ({outcome.reason}). "
            f"The stamper that should have posted it failed — check that run."
        )
    for outcome in failed:
        print(
            f"::error::PR #{outcome.number}: a READY_TO_MERGE verdict is still "
            f"missing its atlan-ci approval and the reconciler could not post it "
            f"either. atlan-ci quota is likely still exhausted."
        )

    if not reconciled and not failed:
        print("Nothing to reconcile.")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path or not (reconciled or failed):
        return
    lines = ["## SDK review approvals reconciled", ""]
    for outcome in reconciled + failed:
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
        "--dry-run",
        action="store_true",
        help="Report which PRs would be reconciled without approving any of them.",
    )
    args = parser.parse_args(argv)

    outcomes = sweep(
        args.repo,
        runner=runner,
        min_age=timedelta(minutes=args.min_age_minutes),
        dry_run=args.dry_run,
    )
    report(outcomes, args.repo)
    return 1 if any(outcome.action == FAILED for outcome in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
