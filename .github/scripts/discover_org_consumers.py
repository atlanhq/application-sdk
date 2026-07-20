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

RunFn = Callable[[list], str]

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


def _run_gh(args: list) -> str:
    """Run `gh` and return stdout, or "" on any failure (missing file, auth,
    network). Callers treat "" as 'no data'.

    On failure, echo `gh`'s stderr to this process's stderr before returning ""
    so a real auth/scope/network error (e.g. a 401 that would otherwise look
    identical to an empty fleet) is diagnosable from the workflow log, rather
    than silently collapsing to "no data"."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        if result.stderr:
            print(
                f"::warning::gh {args[0]} failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
        return ""
    return result.stdout


def parse_repos(raw_output: str) -> list:
    """Parse a JSON array of repo names, tolerating empty/malformed output."""
    try:
        repos = json.loads(raw_output)
    except json.JSONDecodeError:
        return []
    return repos if isinstance(repos, list) else []


def list_candidate_repos(owner: str, name_pattern: str, run: RunFn = _run_gh) -> list:
    """All non-archived `owner` repos whose bare name matches `name_pattern`, as
    'owner/name'. Uses `gh repo list` (deterministic), not code search."""
    raw = run(
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
    extends the shared preset). Fetches the raw file; a missing file / no access
    yields "" -> False."""
    content = run(
        [
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo}/contents/renovate.json",
        ]
    )
    return marker in content


def discover_fleet(
    owner: str,
    name_pattern: str,
    marker: str,
    excludes: set,
    run: RunFn = _run_gh,
) -> list:
    """The sorted list of 'owner/name' repos matching the name pattern, not
    excluded, and extending the preset."""
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
    args = parser.parse_args(argv)

    excluded = set(args.exclude or [])
    repos = discover_fleet(
        args.owner, args.name_pattern, args.preset_marker, excluded, run=run
    )

    if not repos:
        print(
            "::warning::No fleet repos discovered (no atlan-*-app extends the preset). "
            "Check the atlan-app-fleet App installation/permissions.",
            file=sys.stderr,
        )
    else:
        print(f"Discovered {len(repos)} fleet repos", file=sys.stderr)

    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"repos={json.dumps(repos)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
