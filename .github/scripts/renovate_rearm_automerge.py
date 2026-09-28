#!/usr/bin/env python3
"""Re-arm GitHub-native auto-merge on green Renovate PRs that lost it (FND-2940).

Reads what the Renovate dashboard scanner already produced, and acts on one
classification: ``automerge_not_armed``. ``renovate-dashboard.yaml`` runs this
hourly, right after the scanner has written ``repos/<slug>.json``.

Why this exists
---------------
Renovate arms native auto-merge once, when it creates the PR. If a
``merge_group`` check then fails, GitHub records ``RemovedFromMergeQueueEvent``
(reason ``failed_checks``) and turns auto-merge off. Renovate never turns it
back on. Its fallback on later runs is a direct merge, which a queue-protected
branch answers with 405 "Changes must be made through the merge queue". Renovate
logs "GitHub blocking PR merge -- will keep trying" and does so forever. One
transient failure, like a vuln-DB pull rate-limited during a release fan-out,
leaves a green PR that nothing will ever merge.

The same state is reached when the arming call fails at creation (a 403 when
the App's rate-limit budget is spent), so this does not care how the PR got
there, only that it is there.

What it trusts the classifier for
---------------------------------
``automerge_not_armed`` already means: an auto-merge lane, a repo not in soft
mode, no merge conflict, dependency files only, every check green, and
``autoMergeRequest`` null. This script does not re-derive any of that. It does
re-read the few fields that can change in the minutes since the scan (state,
draft, armed), because arming a PR that has just closed or been armed is noise.

What it refuses to do
---------------------
Re-arm a PR the queue has already ejected ``MAX_EJECTIONS`` times. A transient
failure clears on the next attempt; a PR that keeps failing only in the queue
(a conflict with a newer base, a check that only runs on ``merge_group``) is a
real fault. Re-arming it every hour would burn a full queue CI run each time
and hide the fault behind a PR that looks busy. It is reported for a human.

Never fails the job for a PR it could not arm. The dashboard has already
published, and a repair that reds the run on a single 403 would train people to
ignore the run.

Usage::

    GH_TOKEN=... python3 renovate_rearm_automerge.py --out-dir /tmp/renovate-output
    GH_TOKEN=... python3 renovate_rearm_automerge.py --out-dir DIR --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from renovate_fleet_scan import _post_graphql

# Mirrors conformance.renovate.models.BlockingReason.AUTOMERGE_NOT_ARMED. Kept as
# a literal because this script runs as a bare `python3` on the runner, outside
# the uv environment the scanner runs in.
NOT_ARMED = "automerge_not_armed"

# Queue ejections after which a PR is a human's problem rather than a transient.
# Counts every ejection in the PR's life, including the ones that led here: the
# first ejection is usually the transient this script exists for, so 3 allows
# two re-arms before giving up.
MAX_EJECTIONS = 3

# Re-arms attempted per run. Each costs two GraphQL calls against the fleet
# App's hourly budget, the same budget the Renovate sweep needs. A normal hour
# has zero to a handful of candidates; a release fan-out that ejected the fleet
# can have dozens, and those drain over the next few hourly runs (see
# select_batch for why they drain rather than repeat).
DEFAULT_MAX_REARMS = 25

_PR_STATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    autoMergeAllowed
    squashMergeAllowed
    mergeCommitAllowed
    rebaseMergeAllowed
    pullRequest(number: $number) {
      id
      state
      isDraft
      autoMergeRequest { enabledAt }
      # filteredCount, not totalCount: totalCount ignores itemTypes and counts
      # the whole timeline (a PR with 2 ejections read 15 on 2026-09-28).
      ejections: timelineItems(itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) {
        filteredCount
      }
    }
  }
}
"""

_ENABLE_MUTATION = """
mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $pullRequestId, mergeMethod: $mergeMethod}
  ) {
    pullRequest { number }
  }
}
"""

PostFn = Callable[[str, dict], dict]


class Outcome(str, Enum):
    ARMED = "armed"
    WOULD_ARM = "would_arm"  # --dry-run
    ALREADY_ARMED = "already_armed"
    NOT_OPEN = "not_open"
    DRAFT = "draft"
    REPO_DISALLOWS = "repo_disallows_automerge"
    TOO_MANY_EJECTIONS = "too_many_ejections"
    # The fleet App's budget is spent. Every later call this run would fail the
    # same way, so the run stops (see main).
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass(frozen=True)
class Candidate:
    repo: str  # owner/name
    number: int
    url: str


@dataclass(frozen=True)
class Result:
    candidate: Candidate
    outcome: Outcome
    detail: str = ""


def _load(path: str) -> Optional[dict]:
    """Parse one JSON file, or None if it is missing or malformed."""
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def candidates(out_dir: str) -> list[Candidate]:
    """Every open PR in the scanner's per-repo output classified not-armed."""
    repos_dir = os.path.join(out_dir, "repos")
    try:
        names = sorted(os.listdir(repos_dir))
    except OSError:
        return []

    found: list[Candidate] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        report = _load(os.path.join(repos_dir, name))
        if report is None:
            print(f"::warning::unreadable repo report: {name}", file=sys.stderr)
            continue
        repo = report.get("repo")
        if not isinstance(repo, str) or "/" not in repo:
            print(f"::warning::repo report without a repo: {name}", file=sys.stderr)
            continue
        for pr in report.get("openPRs") or []:
            if pr.get("blockingReason") != NOT_ARMED:
                continue
            number = pr.get("number")
            if not isinstance(number, int):
                continue
            found.append(Candidate(repo=repo, number=number, url=pr.get("url", "")))
    return found


def merge_method(repo: dict) -> Optional[str]:
    """The method Renovate itself arms with: squash, else merge, else rebase.

    Mirrors ``initRepo`` in Renovate's GitHub platform, which picks the first
    allowed of the three in that order, so a re-armed PR merges the same way a
    PR that was never ejected would have.
    """
    if repo.get("squashMergeAllowed"):
        return "SQUASH"
    if repo.get("mergeCommitAllowed"):
        return "MERGE"
    if repo.get("rebaseMergeAllowed"):
        return "REBASE"
    return None


def _graphql_errors(response: dict) -> str:
    errors = response.get("errors")
    if not errors:
        return ""
    return "; ".join(str(e.get("message", e)) for e in errors)


def _is_rate_limited(response: Optional[dict], text: str) -> bool:
    """Is this failure the App's budget running out, not a real refusal?

    GitHub signals it two ways. The primary GraphQL limit comes back as an
    ``errors`` entry of type ``RATE_LIMITED``. The secondary limit comes back as
    an HTTP 403, which ``_post_graphql`` does not retry and raises as a
    RuntimeError whose message carries the body ("You have exceeded a secondary
    rate limit"). Either way the message names the rate limit; a permission
    refusal ("Resource not accessible by integration") does not.
    """
    for error in (response or {}).get("errors") or []:
        if isinstance(error, dict) and error.get("type") == "RATE_LIMITED":
            return True
    return "rate limit" in text.lower()


def _failure(candidate: Candidate, response: Optional[dict], detail: str) -> Result:
    outcome = (
        Outcome.RATE_LIMITED if _is_rate_limited(response, detail) else Outcome.ERROR
    )
    return Result(candidate, outcome, detail)


def select_batch(found: list[Candidate], cap: int, run_index: int) -> list[Candidate]:
    """Up to ``cap`` candidates, starting at a window that moves every run.

    A plain ``found[:cap]`` would hand the same PRs to every run. PRs past the
    ejection cap stay classified ``automerge_not_armed`` for good, so 25 of them
    early in the sorted list would take every slot every hour and nothing after
    them would ever be reached. Moving the window by ``cap`` each run reaches
    every candidate within ``ceil(len(found) / cap)`` runs, with no state to
    keep between runs. ``run_index`` is the hour, so consecutive hourly runs get
    consecutive windows.
    """
    if cap <= 0 or not found:
        return []
    if len(found) <= cap:
        return list(found)
    start = (run_index * cap) % len(found)
    rotated = found[start:] + found[:start]
    return rotated[:cap]


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {number}")
    return number


def rearm(
    token: str,
    candidate: Candidate,
    *,
    dry_run: bool = False,
    post: PostFn = _post_graphql,
) -> Result:
    """Re-check one candidate live, then arm it if it is still eligible."""
    owner, name = candidate.repo.split("/", 1)
    try:
        response = post(
            token,
            {
                "query": _PR_STATE_QUERY,
                "variables": {
                    "owner": owner,
                    "name": name,
                    "number": candidate.number,
                },
            },
        )
    except RuntimeError as exc:
        return _failure(candidate, None, f"state query: {exc}")

    errors = _graphql_errors(response)
    repo = (response.get("data") or {}).get("repository") or {}
    pr = repo.get("pullRequest") or {}
    if errors or not pr:
        return _failure(candidate, response, f"state query: {errors or 'no PR'}")

    if pr.get("state") != "OPEN":
        return Result(candidate, Outcome.NOT_OPEN, str(pr.get("state")))
    if pr.get("isDraft"):
        return Result(candidate, Outcome.DRAFT)
    if pr.get("autoMergeRequest"):
        return Result(candidate, Outcome.ALREADY_ARMED)
    if not repo.get("autoMergeAllowed"):
        return Result(candidate, Outcome.REPO_DISALLOWS)
    ejections = (pr.get("ejections") or {}).get("filteredCount", 0)
    if ejections >= MAX_EJECTIONS:
        return Result(
            candidate, Outcome.TOO_MANY_EJECTIONS, f"{ejections} queue ejections"
        )
    method = merge_method(repo)
    if method is None:
        return Result(candidate, Outcome.ERROR, "repo allows no merge method")

    if dry_run:
        return Result(candidate, Outcome.WOULD_ARM, method)

    try:
        response = post(
            token,
            {
                "query": _ENABLE_MUTATION,
                "variables": {"pullRequestId": pr["id"], "mergeMethod": method},
            },
        )
    except RuntimeError as exc:
        return _failure(candidate, None, f"enable: {exc}")
    errors = _graphql_errors(response)
    if errors:
        return _failure(candidate, response, f"enable: {errors}")
    return Result(candidate, Outcome.ARMED, method)


def main(
    argv: Optional[list[str]] = None,
    post: PostFn = _post_graphql,
    run_index: Optional[int] = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        required=True,
        help="directory the renovate-scan CLI wrote (contains repos/)",
    )
    parser.add_argument(
        "--max-rearms",
        # Not plain int: a negative value would turn found[:cap]-style slicing
        # into "all but the last N" and walk straight past the budget cap.
        type=_non_negative_int,
        default=DEFAULT_MAX_REARMS,
        help=f"cap on PRs handled per run (default {DEFAULT_MAX_REARMS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be armed"
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN", "")
    if not token and not args.dry_run:
        print("::error::GH_TOKEN is not set", file=sys.stderr)
        return 2

    found = candidates(args.out_dir)
    if run_index is None:
        run_index = int(time.time() // 3600)
    todo = select_batch(found, args.max_rearms, run_index)
    print(f"not-armed PRs: {len(found)}, handling {len(todo)}")
    if len(found) > len(todo):
        print(
            f"::notice::{len(found) - len(todo)} not-armed PR(s) left for the "
            "next run (per-run cap)"
        )

    results: list[Result] = []
    for index, candidate in enumerate(todo):
        result = rearm(token, candidate, dry_run=args.dry_run, post=post)
        results.append(result)
        if result.outcome is Outcome.RATE_LIMITED:
            # Stop rather than sleep to the reset. Every later call would fail
            # the same way, and waiting would hold this budget against the
            # Renovate sweep that shares it. The rest go to the next hourly run,
            # which is the retry.
            print(
                f"::warning::fleet App rate limit hit; stopping with "
                f"{len(todo) - index - 1} PR(s) left for the next run: "
                f"{result.detail}"
            )
            break
    for r in results:
        detail = f" ({r.detail})" if r.detail else ""
        print(f"  {r.candidate.repo}#{r.candidate.number}  {r.outcome.value}{detail}")

    stuck = [r for r in results if r.outcome is Outcome.TOO_MANY_EJECTIONS]
    for r in stuck:
        print(
            f"::warning::{r.candidate.repo}#{r.candidate.number} was ejected from "
            f"the merge queue {r.detail.split()[0]} times; not re-arming. "
            f"A human needs to look: {r.candidate.url}"
        )
    failed = [r for r in results if r.outcome is Outcome.ERROR]
    for r in failed:
        print(
            f"::warning::could not re-arm {r.candidate.repo}#{r.candidate.number}: "
            f"{r.detail}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
