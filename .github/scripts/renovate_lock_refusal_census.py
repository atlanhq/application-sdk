#!/usr/bin/env python3
"""Census of frozen lock refusals across the fleet (FND-909).

The verdict on whether the lock lane is healthy — and deliberately not the state
of any named PR. `recreateClosed: true` plus a four-hourly cron churns the PR
population faster than a ticket is read: over two days the frozen set held at
five while its membership rotated by four repos. Checking the PR numbers a
ticket happens to name proves only that the lane moved, never that it recovered.
So the signal is the *census*: how many open lock-maintenance PRs carry a
refusal tripwire right now, which repos, and how long each has been stuck.

Usage::

    GITHUB_TOKEN=... python3 renovate_lock_refusal_census.py
    GITHUB_TOKEN=... python3 renovate_lock_refusal_census.py --json

Success criterion after the reaper lands: **no `window-empty` PR survives two
consecutive fleet passes.** The cron is every four hours, so any such PR whose
head is older than ~8h means the reaper did not fire and the fix is not working.
The script exits 1 when it finds one, which is what makes it usable as a check
rather than a report someone has to read.

A `stale_standing` count is reported separately and does *not* fail the run: a
yanked pin or an unsatisfiable floor is meant to stay red until a human clears
it. It is surfaced because a standing fault that sits for days is a different
problem worth seeing, not because the reaper should have taken it.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from renovate_reap_refused_locks import (  # noqa: E402
    BRANCH,
    is_tripwire,
    refusal_reason,
)
from renovate_uv_lock_bounded import SELF_HEALING_REFUSALS  # noqa: E402

API_ROOT = "https://api.github.com"

# Two fleet passes at the 4-hourly cron, plus a pass's own runtime. A
# window-empty refusal older than this was not reaped when it should have been.
SURVIVED_TWO_PASSES = dt.timedelta(hours=9)


def _get(token: str, url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def open_lock_prs(token: str) -> list[dict]:
    """Every open lock-maintenance PR in the org, paginated to completion."""
    out: list[dict] = []
    page = 1
    while True:
        q = urllib.parse.quote(f"org:atlanhq is:pr is:open head:{BRANCH}", safe=":/")
        data = _get(token, f"{API_ROOT}/search/issues?q={q}&per_page=100&page={page}")
        items = data.get("items", [])  # type: ignore[union-attr]
        out.extend(items)
        if len(items) < 100:
            return out
        page += 1
        if page > 10:  # the search API's own 1000-item ceiling
            return out


def inspect(token: str, repo: str, number: int) -> tuple[str, Optional[str], str]:
    """``(verdict, reason, head_committed_at)`` for one PR.

    ``head_committed_at`` is the right clock, not ``created_at``: Renovate
    rewrites a lock branch in place, so a PR opened last week may carry a
    refusal written an hour ago.
    """
    files = _get(token, f"{API_ROOT}/repos/{repo}/pulls/{number}/files?per_page=100")
    names = [f["filename"] for f in files]  # type: ignore[union-attr]
    pr = _get(token, f"{API_ROOT}/repos/{repo}/pulls/{number}")
    head_sha = pr["head"]["sha"]  # type: ignore[index]
    commit = _get(token, f"{API_ROOT}/repos/{repo}/commits/{head_sha}")
    committed = commit["commit"]["committer"]["date"]  # type: ignore[index]
    if len(names) != 1 or names[0].rsplit("/", 1)[-1] != "uv.lock":
        return "ordinary", None, committed
    contents = _get(token, f"{API_ROOT}/repos/{repo}/contents/{names[0]}?ref={BRANCH}")
    text = base64.b64decode(contents["content"]).decode(  # type: ignore[index]
        errors="replace"
    )
    reason = refusal_reason(text)
    if reason is None:
        # Either an ordinary single-file refresh or a pre-FND-909 unstamped
        # tripwire. `is_tripwire` is what tells them apart — the presence of an
        # `[options]` table does not, because uv writes one of its own in any
        # repo that declares a bound in pyproject.toml.
        verdict = "unstamped_tripwire" if is_tripwire(text) else "ordinary"
        return verdict, None, committed
    return "refusal", reason, committed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    rows: list[dict] = []
    for pr in open_lock_prs(token):
        repo = pr["repository_url"].split("/repos/", 1)[1]
        try:
            verdict, reason, committed = inspect(token, repo, pr["number"])
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            print(f"::warning::could not inspect {repo}#{pr['number']}: {exc}")
            continue
        head = dt.datetime.fromisoformat(committed.replace("Z", "+00:00"))
        rows.append(
            {
                "repo": repo,
                "number": pr["number"],
                "verdict": verdict,
                "reason": reason,
                "head_committed_at": committed,
                "age_hours": round((now - head).total_seconds() / 3600, 1),
            }
        )

    frozen = [
        r
        for r in rows
        if r["reason"] in SELF_HEALING_REFUSALS
        and r["age_hours"] > SURVIVED_TWO_PASSES.total_seconds() / 3600
    ]
    standing = [
        r
        for r in rows
        if r["verdict"] == "refusal" and r["reason"] not in SELF_HEALING_REFUSALS
    ]
    unstamped = [r for r in rows if r["verdict"] == "unstamped_tripwire"]

    if args.json:
        print(
            json.dumps(
                {
                    "total_open": len(rows),
                    "frozen_self_healing": frozen,
                    "standing_faults": standing,
                    "unstamped_tripwires": unstamped,
                },
                indent=2,
            )
        )
    else:
        print(f"open lock-maintenance PRs: {len(rows)}")
        print(
            f"  ordinary:            {sum(1 for r in rows if r['verdict'] == 'ordinary')}"
        )
        print(f"  unstamped tripwire:  {len(unstamped)}")
        print(f"  standing faults:     {len(standing)}")
        print(f"  FROZEN self-healing: {len(frozen)}")
        for r in frozen + standing + unstamped:
            print(
                f"    {r['repo']}#{r['number']}  {r['verdict']}"
                f"/{r['reason'] or '-'}  {r['age_hours']}h"
            )

    if frozen:
        print(
            f"::error::{len(frozen)} self-healing refusal(s) survived two fleet "
            "passes — the reaper is not firing",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
