#!/usr/bin/env python3
"""Discover atlanhq repos that depend on atlan-application-sdk, via GitHub code
search. Used by reusable-renovate-dispatch.yaml to build its fan-out matrix.

Extracted from an inline shell if/else per docs/standards/ci.md (no branching
logic in workflow YAML `run:` blocks).

Environment:
    GH_TOKEN   bearer token for `gh` CLI (App installation token or PAT)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Callable, Optional

RunFn = Callable[[list], str]


def _run_gh(args: list) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        return "[]"
    return result.stdout


def parse_repos(raw_output: str) -> list:
    """Parse gh search code's --jq-filtered stdout into a repo list, tolerating
    empty/malformed output (network errors, auth errors, no matches)."""
    try:
        repos = json.loads(raw_output)
    except json.JSONDecodeError:
        return []
    return repos if isinstance(repos, list) else []


def discover_repos(owner: str, query: str, run: RunFn = _run_gh) -> list:
    raw = run(
        [
            "search",
            "code",
            "--owner",
            owner,
            query,
            "--json",
            "repository",
            "--jq",
            "[.[].repository.nameWithOwner] | unique",
            "--limit",
            "100",
        ]
    )
    return parse_repos(raw)


def main(argv: Optional[list] = None, run: RunFn = _run_gh) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--owner", required=True, help="org to search, e.g. 'atlanhq'")
    parser.add_argument(
        "--query",
        required=True,
        help="gh search code query, e.g. 'atlan-application-sdk filename:pyproject.toml'",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="OWNER/REPO",
        help="Repo (owner/repo) to drop from the discovered list; repeatable. "
        "Used to keep a repo on a different Renovate engine — e.g. the "
        "self-hosted fleet runner excludes atlanhq/application-sdk, which stays "
        "on the Mend-hosted app.",
    )
    args = parser.parse_args(argv)

    repos = discover_repos(args.owner, args.query, run=run)

    excluded = set(args.exclude or [])
    if excluded:
        repos = [r for r in repos if r not in excluded]

    if not repos:
        print(
            "::warning::No consumer repos discovered. Check the atlan-app-fleet App installation/permissions.",
            file=sys.stderr,
        )
    else:
        print(f"Discovered {len(repos)} consumer repos", file=sys.stderr)

    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"repos={json.dumps(repos)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
