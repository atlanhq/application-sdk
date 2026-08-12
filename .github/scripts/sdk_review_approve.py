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

Labels
------
Written through the REST labels API rather than `gh pr edit`. Labels are an
Issues-scope resource; a fine-grained token carrying `pull_requests: write` but
not `issues: write` can approve a review yet silently fail every label write.
The old code sent label errors to /dev/null under `|| true`, so that failure was
invisible. Here a delete that 404s is tolerated (the label was already absent)
and anything else is surfaced as a ::warning::.

Environment:
    REPO                        owner/repo (e.g. atlanhq/application-sdk)
    PR_NUMBER                   pull request number
    COMMENT_BODY                body of the triggering verdict comment
    TRIGGERING_COMMENT_ID       id of the triggering comment
    GH_TOKEN                    App installation token — every call but APPROVE
    APPROVER_TOKEN              `atlan-ci` PAT — the APPROVE call only
    APPROVE_MAX_ATTEMPTS        default 3
    APPROVE_MAX_WAIT_SECONDS    total sleep budget across retries, default 120

Exit status:
    0  stamped, or intentionally skipped by a guard
    1  verdict was READY_TO_MERGE but the approval could not be posted
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable

Runner = Callable[..., subprocess.CompletedProcess]
Sleeper = Callable[[float], None]

SUMMARY_MARKERS = ("<!-- SDK_REVIEW -->", "<!-- TEST_SDK_REVIEW -->")

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

    def _paginated(self, path: str) -> list[dict]:
        """Flatten a `--paginate --slurp` response into a list of objects."""
        result = self.run(["api", f"repos/{self.repo}/{path}", "--paginate", "--slurp"])
        if result.returncode != 0:
            print(f"::warning::could not list {path}: {result.stderr.strip()}")
            return []
        try:
            pages = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            print(f"::warning::could not parse {path} ({exc})")
            return []
        items: list[dict] = []
        for page in pages:
            # An un-paginated response is a bare array; --slurp adds one level.
            # Tolerate both shapes.
            if isinstance(page, list):
                items.extend(item for item in page if isinstance(item, dict))
            elif isinstance(page, dict):
                items.append(page)
        return items

    def newest_summary_comment_id(self) -> int:
        """Id of the most recent SDK review summary comment, or 0 if none."""
        ids = [
            comment.get("id", 0)
            for comment in self._paginated(f"issues/{self.pr_number}/comments")
            if any(marker in (comment.get("body") or "") for marker in SUMMARY_MARKERS)
        ]
        return max(ids) if ids else 0

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

    def add_labels(self, labels: set[str]) -> None:
        if not labels:
            return
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
        else:
            print(f"Added labels: {sorted(labels)}")

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

    def bot_approval_ids(self) -> list[int]:
        """Ids of live atlan-ci approvals bearing our signature."""
        return [
            review.get("id")
            for review in self._paginated(f"pulls/{self.pr_number}/reviews")
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
        if is_rate_limited(stderr):
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


def main(
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
    now: Callable[[], float] = time.time,
) -> int:
    repo = os.environ.get("REPO", "")
    pr_number = os.environ.get("PR_NUMBER", "")
    body = os.environ.get("COMMENT_BODY", "")
    triggering_id = int(os.environ.get("TRIGGERING_COMMENT_ID") or 0)
    approver_token = os.environ.get("APPROVER_TOKEN", "")
    max_attempts = int(os.environ.get("APPROVE_MAX_ATTEMPTS") or 3)
    max_wait = float(os.environ.get("APPROVE_MAX_WAIT_SECONDS") or 120)

    verdict = extract_verdict(body)
    if not verdict:
        print(
            f"::warning::PR #{pr_number}: could not extract a verdict from the "
            f"mothership-ai comment. Skipping."
        )
        return 0
    print(f"Detected verdict: '{verdict}'")

    # A comment with no REVIEWED_HEAD predates the stamp; its reviewed sha
    # cannot be established, and approving blind is worse than not approving.
    reviewed_head = extract_reviewed_head(body)
    if not reviewed_head:
        print(
            f"::warning::PR #{pr_number}: verdict comment has no REVIEWED_HEAD "
            f"marker — cannot confirm which sha was reviewed; skipping all stamps."
        )
        return 0

    client = Client(repo, pr_number, runner)

    head_sha = client.head_sha()
    if not head_sha:
        print(f"::warning::PR #{pr_number}: HEAD unresolvable — skipping all stamps.")
        return 0

    # The verdict was computed for reviewed_head. A push since then means the
    # current head is unreviewed, and stamping it would bless unreviewed code.
    if reviewed_head != head_sha:
        print(
            f"::warning::PR #{pr_number}: verdict was computed for {reviewed_head} "
            f"but current head is {head_sha} — skipping all stamps."
        )
        return 0

    # The job's concurrency group serialises runs per PR, but GitHub's queue is
    # not event-time ordered: after waiting, this run may execute after a newer
    # verdict has already been stamped. Ids are monotonic, so a larger one means
    # a newer summary exists and owns the final say.
    newest_id = client.newest_summary_comment_id()
    if newest_id > triggering_id:
        print(
            f"::notice::PR #{pr_number}: a newer SDK_REVIEW comment "
            f"(id={newest_id}) supersedes this one (id={triggering_id}) — skipping."
        )
        return 0

    to_add, to_remove = label_plan(verdict, client.current_labels())
    client.add_labels(to_add)
    for label in sorted(to_remove):
        client.remove_label(label)

    state, description = status_for(verdict)

    if verdict != READY:
        # sdk-review-dismiss-on-human.yml only fires on HUMAN activity, so when a
        # newer verdict supersedes a prior READY_TO_MERGE the stale bot approval
        # has to be cleared here or the merge gate stays open on it.
        stale = client.bot_approval_ids()
        if stale:
            message = (
                f"Newer SDK review verdict ({verdict}) supersedes the prior bot "
                f"approval — auto-dismissing. Re-run `@sdk-review` when ready for "
                f"re-approval."
            )
            for review_id in stale:
                client.dismiss(review_id, message)
        client.post_status(head_sha, state, description)
        print(f"Verdict '{verdict}' is not READY TO MERGE — no approval posted.")
        return 0

    # Don't double-approve: sdk-review.yml's slow path runs the same stamp after
    # the SSE stream closes.
    if client.bot_approval_ids():
        print(
            f"atlan-ci has already approved PR #{pr_number} with the bot "
            f"signature — skipping."
        )
        client.post_status(head_sha, state, description)
        return 0

    if not approver_token:
        print("::error::APPROVER_TOKEN is not set — cannot post a codeowner approval.")
        return 1

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
        # Leave the commit status as the dispatcher set it (pending). Going green
        # here is what made this failure look like success on the PR.
        print(
            f"::error::PR #{pr_number}: verdict is READY_TO_MERGE but the atlan-ci "
            f"approval could not be posted. The sdk-review status is left pending so "
            f"the merge gate stays blocked. Re-run this workflow, or comment "
            f"`@sdk-review`, once quota has recovered."
        )
        return 1

    print(f"Approved PR #{pr_number} as atlan-ci (commit_id={head_sha}).")
    client.post_status(head_sha, state, description)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
