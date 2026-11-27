#!/usr/bin/env python3
"""Fail the dashboard run when this repo's lock-maintenance PR has gone stale (FND-5471).

Reads what the Renovate dashboard scanner already produced, the same way
``renovate_refusal_alarm.py`` does: no GitHub API calls, no second fleet walk.

Why this exists
---------------
application-sdk's lock refresh merges several times a day when it works. When it
stops, nothing goes red. The PR stays open, auto-merge stays armed, and the
lock simply stops moving. That happened for 22 days after bot commits started
carrying ``atlan-ci`` as their committer: Renovate read the cooldown's fix-up
commit as a human edit and stopped rebasing the branch, so the PR could neither
refresh nor merge. Drift then piles up into one large forced bump, and the SDK's
CI stops testing the transitive versions connector apps get on install.

The cause does not matter to this alarm. A lock-maintenance PR older than
:data:`MAX_AGE_DAYS` means the lane is stuck, whatever stuck it.

Whose PRs fail the run
----------------------
Only those of the repos named with ``--fail-repo``, which the workflow sets to
``github.repository``: the lane this repo owns and nobody else watches. A stale
lock PR in a connector repo is printed and counted in one warning, and does not
fail the run. Those PRs are owned by their repos, are already on the dashboard,
and are often stale for reasons a human is working through (an app's own tests
failing on the refresh, a standing refusal). An hourly job that was red on them
would always be red, and an alarm that is always red stops being read.

A ``--fail-repo`` the scanner wrote no report for fails the run too. An absent
report is not a healthy one: it is what a scan that silently dropped the repo
looks like.

Usage::

    python3 renovate_stale_lock_alarm.py --out-dir /tmp/renovate-output \\
        --fail-repo atlanhq/application-sdk
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, TypedDict, cast

#: Mirrors conformance.renovate.models.Category.LOCK_MAINTENANCE. A literal for
#: the same reason as renovate_refusal_alarm.py: this runs as a bare `python3`,
#: outside the uv environment the scanner runs in. A test pins the two equal.
LOCK_MAINTENANCE = "lock-maintenance"

#: A lock-maintenance PR at least this many whole days old is stale. The lane
#: merges within hours when healthy, so a week is far past any slow but working
#: path (a fleet pass delayed by status-success, a release freeze).
MAX_AGE_DAYS = 7


class StalePR(TypedDict):
    repo: str
    number: object
    url: str
    ageDays: int
    blockingReason: str


def _load(path: str) -> Optional[dict[str, object]]:
    """Parse one JSON object, or None if it is missing or malformed."""
    try:
        with open(path, encoding="utf-8") as handle:
            loaded: object = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return cast("dict[str, object]", loaded)


def _open_prs(report: dict[str, object]) -> list[dict[str, object]]:
    """The report's ``openPRs`` entries that are objects; anything else is skipped."""
    prs = report.get("openPRs")
    if not isinstance(prs, list):
        return []
    return [
        cast("dict[str, object]", pr)
        for pr in cast("list[object]", prs)
        if isinstance(pr, dict)
    ]


def scan(out_dir: str, max_age_days: int) -> tuple[set[str], list[StalePR]]:
    """Every repo the scanner reported on, and every stale lock PR among them.

    Repo names are lower-cased: GitHub compares them case-insensitively, and
    ``--fail-repo`` comes from a different source than the scanner's output.
    """
    repos_dir = os.path.join(out_dir, "repos")
    try:
        names = sorted(os.listdir(repos_dir))
    except OSError:
        return set(), []

    reported: set[str] = set()
    stale: list[StalePR] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        report = _load(os.path.join(repos_dir, name))
        if report is None:
            print(f"::warning::unreadable repo report: {name}", file=sys.stderr)
            continue
        repo = str(report.get("repo") or name[: -len(".json")]).lower()
        reported.add(repo)
        for pr in _open_prs(report):
            if pr.get("category") != LOCK_MAINTENANCE:
                continue
            age = pr.get("ageDays")
            if not isinstance(age, int) or age < max_age_days:
                continue
            stale.append(
                StalePR(
                    repo=repo,
                    number=pr.get("number"),
                    url=str(pr.get("url") or ""),
                    ageDays=age,
                    blockingReason=str(pr.get("blockingReason") or ""),
                )
            )
    return reported, stale


def _describe(pr: StalePR) -> str:
    return (
        f"  {pr['repo']}#{pr['number']}  age={pr['ageDays']}d"
        f"  blocking={pr['blockingReason'] or '-'}\n    {pr['url']}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        required=True,
        help="directory the renovate-scan CLI wrote (contains repos/)",
    )
    parser.add_argument(
        "--fail-repo",
        action="append",
        required=True,
        help="owner/name whose stale lock PR fails the run; repeatable",
    )
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    args = parser.parse_args(argv)

    owned = {repo.lower() for repo in args.fail_repo}
    reported, stale = scan(args.out_dir, args.max_age_days)
    failing = [pr for pr in stale if pr["repo"] in owned]
    elsewhere = [pr for pr in stale if pr["repo"] not in owned]
    unreported = sorted(owned - reported)

    print(f"stale lock-maintenance PRs here (fatal): {len(failing)}")
    for pr in failing:
        print(_describe(pr))
    print(f"stale lock-maintenance PRs elsewhere (not alarmed): {len(elsewhere)}")
    for pr in elsewhere:
        print(_describe(pr))

    if elsewhere:
        print(
            f"::warning::{len(elsewhere)} lock-maintenance PR(s) in other repos "
            f"are {args.max_age_days}+ days old; each is owned by its repo"
        )
    if unreported:
        print(
            f"::error::the scanner wrote no report for {', '.join(unreported)}, "
            "so its lock lane cannot be checked",
            file=sys.stderr,
        )
    if failing:
        print(
            f"::error::{len(failing)} lock-maintenance PR(s) here are "
            f"{args.max_age_days}+ days old: the lock refresh lane is stuck "
            "(FND-5471)",
            file=sys.stderr,
        )
    return 1 if failing or unreported else 0


if __name__ == "__main__":
    raise SystemExit(main())
