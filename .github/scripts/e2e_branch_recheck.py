#!/usr/bin/env python3
"""Has the branch a dispatched e2e run is testing moved on? (FND-3839)

A `/e2e` or `/gate-check` comment dispatches the connector's `tests.yaml` on a
PR branch, and the run tests the branch tip as it was at dispatch
(`github.sha`). If someone pushes while that run is building its image, the
commit it is about to spend tenant time on is no longer the PR's head: nobody
can merge it, and its verdict lands on a commit nobody will read. The SDK's
cross-repo dispatch already stands down in exactly this case
(e2e_dispatch_recheck.py, FND-701); this is the same question for a run the
connector dispatched for itself.

Asked immediately before the tenant lease, so a superseded run never queues
for a tenant and the newest commit's run gets it instead. The Tests Gate reads
the same `superseded` output and reports the skipped matrix as a stand-down,
not an anomaly.

Prints ``superseded=true`` only when the branch tip is positively a different
commit. Every doubt — not a dispatch, the branch unreadable, a malformed
answer — prints ``superseded=false``: wrongly standing down denies a live
commit its e2e, while wrongly carrying on only costs tenant time. Always exits
0.

Tested in tests/test_e2e_branch_recheck.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Callable
from urllib.parse import quote

GhFn = Callable[[list], subprocess.CompletedProcess]


#: The process runner behind :func:`_gh`. A module attribute so a test can
#: replace it here without touching the shared stdlib ``subprocess`` module.
_RUNNER: Callable[..., subprocess.CompletedProcess] = subprocess.run


def _gh(args: list) -> subprocess.CompletedProcess:
    try:
        return _RUNNER(["gh", *args], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["gh", *args], 124, "", "timed out")


def branch_tip(repo: str, branch: str, gh: GhFn = _gh) -> str | None:
    result = gh(["api", f"repos/{repo}/branches/{quote(branch, safe='')}"])
    if result.returncode != 0:
        print(
            f"::warning::could not read branch {branch!r}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        sha = json.loads(result.stdout)["commit"]["sha"]
    except (ValueError, KeyError, TypeError):
        return None
    return sha if isinstance(sha, str) and len(sha) == 40 else None


def superseded(
    repo: str, event_name: str, branch: str, tested_sha: str, gh: GhFn = _gh
) -> bool:
    if event_name != "workflow_dispatch" or not branch or len(tested_sha) != 40:
        return False
    tip = branch_tip(repo, branch, gh)
    if tip is None:
        return False
    if tip != tested_sha:
        print(
            f"::notice::{branch} moved from {tested_sha[:7]} to {tip[:7]}; "
            "standing this e2e run down so the newer commit gets the tenant",
            file=sys.stderr,
        )
        return True
    return False


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--branch", default="")
    parser.add_argument("--tested-sha", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    answer = superseded(args.repo, args.event_name, args.branch, args.tested_sha)
    print(f"superseded={'true' if answer else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
