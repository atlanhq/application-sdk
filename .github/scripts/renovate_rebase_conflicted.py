#!/usr/bin/env python3
"""Backstop: re-run Renovate on fleet repos whose Renovate PR is in conflict.

Run by ``.github/workflows/renovate-rebase-conflicted.yaml`` on a short cron.

Why (FND-3481)
--------------
Renovate only rebuilds a conflicted branch when it runs on that repo, and the
scheduled sweep is every four hours. GitHub runs no ``pull_request`` workflows
on a PR that conflicts with its base, so until then the PR shows no fresh
checks and cannot auto-merge. Every release used to start that wait over: the
framework-deps PR merged, the lock-maintenance PR went into conflict, and sat.

The primary fix is upstream of this — the lock-refresh lane now holds every
package the framework lane moves (``renovate_uv_lock_bounded --hold``), so the
two PRs should not overlap at all. This is the second layer for whatever still
conflicts: a race between two releases, a replay that could not run, or an
overlap nobody has thought of. It turns "up to four hours" into "one tick".

What it does
------------
1. One paginated GraphQL search for open PRs authored by the fleet App
   (``app/atlan-app-fleet``) whose ``mergeable`` is ``CONFLICTING``.
   App authorship alone is NOT the allowlist: ``renovate.yaml``'s ``repos``
   input feeds its matrix directly, bypassing the discovery filter, and a repo
   that has left the fleet can still carry an old fleet-App PR. So each
   candidate is then checked for fleet membership (``fleet_member``: the
   ``atlan-*-app`` name, and the shared preset as an exact ``extends`` entry —
   stricter than discovery's substring test, see ``FLEET_PRESET``) — one read
   per conflicted repo, paid only when something is conflicted.
2. If any ``renovate.yaml`` run is queued or in progress, it does nothing — a
   live sweep reaches these repos anyway, and an earlier dispatch from this
   backstop is still working on them. Skipping is what keeps this from piling
   scoped runs onto the fleet App's hourly API budget while a slow one finishes.
3. Otherwise dispatches ONE ``renovate.yaml`` run scoped to those repos via its
   ``repos`` input. Scoped runs take a run-unique concurrency group, so they
   never cancel a fleet sweep.

``mergeable`` is computed lazily by GitHub and reads ``UNKNOWN`` until
something asks; the search itself asks, so a PR missed as UNKNOWN on one tick
is seen on the next.

Usage:
    python3 .github/scripts/renovate_rebase_conflicted.py [--dry-run]

Env: ``FLEET_TOKEN`` (fleet App installation token: org-wide PR search) and
``GH_TOKEN`` (this repo's token with ``actions: write``: run listing and
dispatch). ``GITHUB_REPOSITORY`` names the repo that owns ``renovate.yaml``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discover_org_consumers as discover  # noqa: E402
import renovate_fleet_scan as scan  # noqa: E402

FLEET_AUTHOR = "app/atlan-app-fleet"
WORKFLOW = "renovate.yaml"
EXCLUDE_REPOS = frozenset({"atlanhq/application-sdk"})
BUSY_STATUSES = ("queued", "in_progress")

_FIELDS = """
mergeable
headRefName
repository { nameWithOwner }
"""

Runner = Callable[..., subprocess.CompletedProcess]
Fetch = Callable[[str, str, str], list[dict]]
IsMember = Callable[[str], bool]

_FLEET_NAME = re.compile(discover.DEFAULT_NAME_PATTERN)

# The exact `extends` entry every fleet repo carries — all 83 on 2026-10-07, no
# variants. Matched as a whole list element, not as a substring of the file:
# `discover.extends_preset`'s substring test would also admit
# `github>other-owner/application-sdk//renovate-config/default.json`, or the
# path quoted in a description, and this list decides who a dispatch reaches.
FLEET_PRESET = f"github>atlanhq/{discover.PRESET_MARKER}"


def fleet_member(repo: str, run_gh: discover.RunFn) -> bool:
    """The fleet-membership rule, applied to one repo: an ``atlan-*-app`` name,
    and a renovate.json whose top-level ``extends`` lists ``FLEET_PRESET``.

    Raises (via ``discover.DiscoveryError``) when the config cannot be read for
    any reason but a 404: an unanswerable check must stop the dispatch, not
    quietly admit or drop the repo. A config that is not a JSON object is not a
    member — Renovate itself would reject it, so there is nothing to rebase.
    """
    if not _FLEET_NAME.match(repo.split("/", 1)[-1]):
        return False
    text = discover.read_renovate_config(repo, run=run_gh)
    if text is None:
        return False
    try:
        config = json.loads(text)
    except json.JSONDecodeError:
        return False
    extends = config.get("extends") if isinstance(config, dict) else None
    return isinstance(extends, list) and FLEET_PRESET in extends


def gh_as(token: str) -> discover.RunFn:
    """A ``discover`` gh runner authenticated as ``token`` (the fleet App, which
    can read every fleet repo's renovate.json; this repo's GITHUB_TOKEN cannot)."""

    def run_gh(args: list) -> tuple:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GH_TOKEN": token},
        )
        return result.returncode, result.stdout, result.stderr

    return run_gh


def search_query(org: str) -> str:
    return f"org:{org} is:pr is:open author:{FLEET_AUTHOR}"


def conflicted_repos(prs: list[dict]) -> list[str]:
    """Sorted, de-duplicated repos with at least one conflicted Renovate PR."""
    repos: set[str] = set()
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("mergeable") != "CONFLICTING":
            continue
        if not str(pr.get("headRefName") or "").startswith("renovate/"):
            continue
        repo = (pr.get("repository") or {}).get("nameWithOwner")
        if isinstance(repo, str) and repo and repo not in EXCLUDE_REPOS:
            repos.add(repo)
    return sorted(repos)


def _gh(args: list[str], runner: Runner) -> str:
    result = runner(["gh", *args], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"gh {args[0]} failed: {(result.stderr or '')[-300:]}")
    return result.stdout or ""


def renovate_busy(repo: str, runner: Runner) -> bool:
    """True when any renovate.yaml run in ``repo`` is queued or in progress."""
    for status in BUSY_STATUSES:
        out = _gh(
            [
                "api",
                f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?status={status}&per_page=1",
                "--jq",
                ".total_count",
            ],
            runner,
        )
        if int(out.strip() or "0") > 0:
            return True
    return False


def dispatch(repo: str, targets: list[str], runner: Runner) -> None:
    _gh(
        [
            "workflow",
            "run",
            WORKFLOW,
            "--repo",
            repo,
            "-f",
            f"repos={json.dumps(targets)}",
        ],
        runner,
    )


def run(
    *,
    org: str,
    home_repo: str,
    fleet_token: str,
    dry_run: bool,
    fetch: Fetch,
    runner: Runner,
    is_member: IsMember,
) -> list[str]:
    """Returns the repos dispatched (empty when there was nothing to do)."""
    conflicted = conflicted_repos(fetch(fleet_token, search_query(org), _FIELDS))
    targets = [repo for repo in conflicted if is_member(repo)]
    outside = sorted(set(conflicted) - set(targets))
    if outside:
        print(f"Not in the Renovate fleet, skipped: {', '.join(outside)}")
    if not targets:
        print("No conflicted fleet Renovate PRs.")
        return []
    print(f"Conflicted Renovate PRs in {len(targets)} repo(s): {', '.join(targets)}")
    if renovate_busy(home_repo, runner):
        print(
            f"{WORKFLOW} is already queued or running; it (or the next tick) "
            "will reach these repos. Not dispatching."
        )
        return []
    if dry_run:
        print("Dry run: not dispatching.")
        return []
    dispatch(home_repo, targets, runner)
    print(f"Dispatched {WORKFLOW} scoped to {len(targets)} repo(s).")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default="atlanhq")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    fleet_token = os.environ.get("FLEET_TOKEN", "")
    home_repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not fleet_token or not home_repo:
        print("FLEET_TOKEN and GITHUB_REPOSITORY are required.", file=sys.stderr)
        return 1
    run(
        org=args.org,
        home_repo=home_repo,
        fleet_token=fleet_token,
        dry_run=args.dry_run,
        fetch=lambda token, query, fields: scan.fetch_all_prs(
            token, query, fields, page_size=100
        ),
        runner=subprocess.run,
        is_member=lambda repo: fleet_member(repo, gh_as(fleet_token)),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
