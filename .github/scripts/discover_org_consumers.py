#!/usr/bin/env python3
"""Discover the atlan-*-app repos the self-hosted Renovate runner should manage:
those whose ``renovate.json`` extends the shared fleet preset.

Deterministic by design. Enumerates repos with ``gh repo list`` (stable) rather
than ``gh search code`` (best-effort and nondeterministic — during a fleet
rehearsal it returned anywhere from 30 to ~90 results across calls and silently
dropped a real consumer). It then keeps only repos whose ``renovate.json``
extends ``application-sdk//renovate-config/default.json``. Repos that have not
adopted the preset are left out (the runner also sets ``onboarding=false``, so an
un-adopted repo is never onboarded even if it slipped in).

The atlan-*-app name filter naturally excludes ``application-sdk`` and the
read-only connector mirror monorepos (``connectors-sql`` / ``-api`` /
``-pipeline``), which also extend the preset but must not be managed here;
``--exclude`` is kept as a belt-and-suspenders override.

A read it cannot complete is a hard failure, never a smaller fleet: only a 404
counts as "this repo has no renovate.json", and any other error raises
``DiscoveryError``. Callers bound their scope by this output — the dashboard
publishes exactly these repos, and the unlisted dashboard sweep DELETES what is
absent from them — so a truncated roster is worse than no roster.

Extracted from inline shell per docs/standards/ci.md (no branching logic in
workflow ``run:`` blocks); unit-tested in tests/test_discover_org_consumers.py.

Environment:
    GH_TOKEN   bearer token for `gh` CLI (atlan-app-fleet installation token)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Callable, Optional

# (args) -> (returncode, stdout, stderr). Same seam shape as
# sweep_dashboard_repos.py. The exit code is part of the contract: a discovery
# that cannot tell "this repo has no renovate.json" from "the API would not
# answer" silently shrinks the fleet, and a roster that quietly lost repos is a
# deletion criterion in sweep_unlisted mode.
RunFn = Callable[[list], tuple]

# Every consumer's renovate.json extends the shared preset via this path; its
# presence is the definitive "on the fleet Renovate policy" signal.
PRESET_MARKER = "application-sdk//renovate-config/default.json"

# Fleet membership is the atlan-*-app naming convention. This deliberately
# excludes application-sdk and the connector mirror monorepos, none of which
# match the pattern.
DEFAULT_NAME_PATTERN = r"^atlan-[a-z0-9-]+-app$"

# `gh repo list` returns at most --limit repos, ordered most-recently-pushed
# first, so a cap below the org's repo count would silently drop an
# infrequently-pushed atlan-*-app from the window. Set well above atlanhq's
# total repo count (hundreds, not thousands); list_candidate_repos emits a loud
# ::warning:: if the org ever grows into this cap, turning a silent drop into an
# observable signal rather than quietly undercutting determinism.
REPO_LIST_LIMIT = 5000


class DiscoveryError(RuntimeError):
    """The fleet could not be determined, as opposed to being determined empty.

    Raised rather than returning a short list, because every caller's failure
    mode for a truncated roster is worse than for no roster at all: the
    dashboard drops rows, and the unlisted sweep would read the omitted repos as
    "does not belong" and delete their data.
    """


def _run_gh(args: list) -> tuple:
    """Run `gh` and return (returncode, stdout, stderr) for the caller to judge.

    Deliberately does no interpretation: only the caller knows whether a
    non-zero exit is an expected answer (no renovate.json) or a reason to stop
    (auth, rate limit, network)."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def _is_not_found(stderr: str) -> bool:
    """True when `gh api` failed because the resource does not exist.

    `gh api` reports a 404 as `gh: Not Found (HTTP 404)` on stderr with exit 1,
    and offers no machine-readable status without re-requesting with `-i`, so
    the status line is the signal available. Matching is narrow on purpose: any
    other failure (401/403/5xx/network) must NOT be read as "file absent".

    A 404 here is a genuine absence rather than a permissions answer, because
    the repo came back from `gh repo list` under this same token — the token can
    see the repo, so it can see whether the file is there.
    """
    return "HTTP 404" in stderr


def parse_repos(raw_output: str) -> list:
    """Parse a JSON array of repo names, tolerating empty/malformed output."""
    try:
        repos = json.loads(raw_output)
    except json.JSONDecodeError:
        return []
    return repos if isinstance(repos, list) else []


def list_candidate_repos(owner: str, name_pattern: str, run: RunFn = _run_gh) -> list:
    """All non-archived `owner` repos whose bare name matches `name_pattern`, as
    'owner/name'. Uses `gh repo list` (deterministic), not code search.

    Raises DiscoveryError if the listing fails. There is no such thing as a
    partial answer here: an org with no repos is not a case that occurs, so an
    empty or unreadable listing is always a token/network problem.
    """
    code, raw, stderr = run(
        [
            "repo",
            "list",
            owner,
            "--no-archived",
            "--limit",
            str(REPO_LIST_LIMIT),
            "--json",
            "nameWithOwner",
            "--jq",
            "[.[].nameWithOwner]",
        ]
    )
    if code != 0:
        raise DiscoveryError(f"gh repo list {owner} failed: {stderr.strip()}")
    all_repos = parse_repos(raw)
    if len(all_repos) >= REPO_LIST_LIMIT:
        print(
            f"::warning::gh repo list returned {len(all_repos)} repos, hitting the "
            f"--limit {REPO_LIST_LIMIT} cap; discovery may be truncated and a "
            "consumer silently dropped. Raise REPO_LIST_LIMIT.",
            file=sys.stderr,
        )
    pat = re.compile(name_pattern)
    return [r for r in all_repos if pat.match(r.split("/", 1)[-1])]


def extends_preset(repo: str, marker: str, run: RunFn = _run_gh) -> bool:
    """True if repo's default-branch renovate.json contains `marker` (i.e.
    extends the shared preset).

    A 404 is a real "no renovate.json" and returns False. Every other failure
    raises DiscoveryError: it says nothing about the repo, and treating it as
    False drops a live consumer out of the roster. That was harmless when the
    roster only fed a dashboard, but the roster is now also the unlisted sweep's
    deletion criterion, where a dropped repo means deleted data.
    """
    code, content, stderr = run(
        [
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/renovate.json",
        ]
    )
    if code != 0:
        if _is_not_found(stderr):
            return False
        raise DiscoveryError(f"reading {repo}/renovate.json failed: {stderr.strip()}")
    return marker in content


def discover_fleet(
    owner: str,
    name_pattern: str,
    marker: str,
    excludes: set,
    run: RunFn = _run_gh,
) -> list:
    """The sorted list of 'owner/name' repos matching the name pattern, not
    excluded, and extending the preset.

    Raises DiscoveryError rather than returning a partial fleet — see
    extends_preset. Aborting on the first unreadable repo is deliberate: there
    is no useful way to consume "the fleet, minus an unknown number of repos we
    could not check".
    """
    candidates = list_candidate_repos(owner, name_pattern, run=run)
    fleet = [
        r
        for r in candidates
        if r not in excludes and extends_preset(r, marker, run=run)
    ]
    return sorted(fleet)


def main(argv: Optional[list] = None, run: RunFn = _run_gh) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--owner", required=True, help="org to enumerate, e.g. 'atlanhq'"
    )
    parser.add_argument(
        "--name-pattern",
        default=DEFAULT_NAME_PATTERN,
        help=f"regex the bare repo name must match (default: {DEFAULT_NAME_PATTERN}).",
    )
    parser.add_argument(
        "--preset-marker",
        default=PRESET_MARKER,
        help="string required in a repo's renovate.json to count it as on the "
        f"fleet Renovate policy (default: {PRESET_MARKER}).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="OWNER/REPO",
        help="Repo (owner/repo) to drop even if it matches; repeatable. Keeps a "
        "repo on a different engine (e.g. atlanhq/application-sdk stays on the "
        "Mend-hosted app).",
    )
    parser.add_argument(
        "--fail-on-empty",
        action="store_true",
        help="exit non-zero when discovery finds no repo. Read failures already "
        "abort on their own; this covers the remaining way to get an empty "
        "answer — a --name-pattern or --preset-marker that matches nothing. A "
        "caller whose whole scope comes from this output should red the run "
        "rather than proceed against an empty fleet.",
    )
    args = parser.parse_args(argv)

    excluded = set(args.exclude or [])
    try:
        repos = discover_fleet(
            args.owner, args.name_pattern, args.preset_marker, excluded, run=run
        )
    except DiscoveryError as exc:
        # No `repos=` output at all. A truncated roster is worse than none: the
        # step's consumers either bound their scope by this output or use it as a
        # deletion criterion, and both are safe against a missing output (`||`
        # falls through, an absent roster refuses) but not against a short one.
        print(f"::error::Fleet discovery failed: {exc}", file=sys.stderr)
        return 1

    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"repos={json.dumps(repos)}\n")

    if not repos:
        # Reaching here means the reads all succeeded and nothing matched, since
        # a failed read raises. So this is a pattern/marker problem, not a token
        # one. Written to the output first either way, so a caller that tolerates
        # an empty fleet still sees `repos=[]` rather than an unset output.
        empty = (
            "No fleet repos discovered: every repo matching --name-pattern was "
            "read successfully and none extends the preset. Check the pattern "
            "and --preset-marker."
        )
        if args.fail_on_empty:
            print(f"::error::{empty} Refusing to proceed.", file=sys.stderr)
            return 1
        print(f"::warning::{empty}", file=sys.stderr)
        return 0

    print(f"Discovered {len(repos)} fleet repos", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
