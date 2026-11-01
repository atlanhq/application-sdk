#!/usr/bin/env python3
"""Decide whether a merge-queue entry's tests were already proven (FND-4344).

A release (bump-version) PR's full suite runs before it is queued: `/gate-check`
dispatches the connector's `tests.yaml` on the PR head, and once every gated
run passes, `gate_check_verdict.py` records a `gate-check` status of
`success` on that commit. `/gate-check` refuses a branch that is behind its
base, so the dispatched run tested the head's own tree. When the queue
commit's tree is that same tree, re-running Unit and Integration in the queue
can only repeat the verdict, so `tests-reusable.yaml` skips them.

This script is the second half of that decision. The first is
queue_tree_diff.py, whose ``identical`` and ``pr_head`` outputs come in here.
It writes ``proven=true`` to ``$GITHUB_OUTPUT`` only when:

* ``identical`` is ``true``: the queue tree is byte-for-byte the PR head's,
  and the head already contains the queue's base; and
* the newest ``gate-check`` status that ``github-actions[bot]`` posted on that
  PR head SHA is ``success``. That is the same rule the connector's Release
  Gate applies (FND-3947): anyone with write access can post a status through
  the API, but only the workflow token posts as github-actions[bot].

The status is read on ``pr_head``, the exact commit whose tree was compared,
so the verdict and the tree belong to one commit.

**Fails safe.** Anything else, including a missing or malformed SHA, an API
error, a timeout, or no such status, writes ``proven=false``, and the queue
entry runs every test as before. A wrong ``true`` would green the required
``Tests Gate`` on a tree nothing tested, so errors only push towards more work.

Extracted from inline shell per docs/standards/ci.md; unit-tested in
tests/test_queue_gate_check_proof.py.

Environment:
    IDENTICAL       queue_tree_diff.py's ``identical`` output.
    PR_HEAD         queue_tree_diff.py's ``pr_head`` output.
    REPO            ``github.repository``.
    GH_TOKEN        a token with ``statuses: read`` (read by ``gh``).
    GITHUB_OUTPUT   the step output file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

#: Reads the ``<state> <creator login>`` lines for one commit's ``gate-check``
#: statuses, newest first.
StatusFn = Callable[[str, str], str]

GATE_CHECK_CONTEXT = "gate-check"
VERDICT_CREATOR = "github-actions[bot]"

#: A stalled API call must fail over to "run everything" well inside the
#: job's five-minute limit.
GH_TIMEOUT_S = 60

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Proof:
    proven: bool
    reason: str


def _read_statuses(repo: str, sha: str) -> str:
    """The commit's ``gate-check`` statuses as ``<state> <creator>`` lines.

    The per-commit statuses list, newest first, as Release Gate reads it; the
    combined status would keep only one row per context, not its creator."""
    return subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{sha}/statuses?per_page=100",
            "--paginate",
            "--jq",
            f'.[] | select(.context == "{GATE_CHECK_CONTEXT}") '
            '| "\\(.state) \\(.creator.login)"',
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_S,
    ).stdout


def verdict_state(lines: str) -> str:
    """The newest state ``VERDICT_CREATOR`` posted, else ``""``.

    A line from anyone else, or in any other shape, is skipped."""
    for line in lines.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == VERDICT_CREATOR:
            return fields[0]
    return ""


def decide(
    identical: str, pr_head: str, repo: str, statuses: StatusFn = _read_statuses
) -> Proof:
    if identical.strip() != "true":
        return Proof(False, "the queue tree is not the PR head's tree")
    if not _SHA_RE.fullmatch(pr_head):
        return Proof(False, f"PR head {pr_head!r} is not a full commit sha")
    if not _REPO_RE.fullmatch(repo):
        return Proof(False, f"repository {repo!r} is not owner/name")
    try:
        state = verdict_state(statuses(repo, pr_head))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return Proof(False, f"could not read statuses on {pr_head}: {exc}")
    if state != "success":
        return Proof(
            False,
            f"no {GATE_CHECK_CONTEXT} success from {VERDICT_CREATOR} on PR head "
            f"{pr_head} (newest: {state or 'none'})",
        )
    return Proof(
        True,
        f"{GATE_CHECK_CONTEXT} passed on PR head {pr_head} and the queue tree is "
        "its tree; its Unit and Integration verdicts stand",
    )


def main(env: dict[str, str] | None = None, statuses: StatusFn = _read_statuses) -> int:
    env = dict(os.environ) if env is None else env
    proof = decide(
        env.get("IDENTICAL", ""),
        env.get("PR_HEAD", "").strip(),
        env.get("REPO", "").strip(),
        statuses=statuses,
    )
    print(f"::notice title=Merge-queue release proof::{proof.reason}")
    line = f"proven={'true' if proof.proven else 'false'}\n"
    output_path = env.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(line)
    sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
