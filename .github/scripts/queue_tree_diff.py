#!/usr/bin/env python3
"""Decide what a merge-queue entry still has to re-check (FND-3321).

A queue entry exists to catch what changes when a PR is combined with the
latest base. A check whose inputs are the same on the queue commit as on the
PR head would only repeat a verdict the PR already earned: a PR cannot enter
the queue until its required checks are green on its head. This script works
out, for one ``merge_group`` run, whether the combination changed anything a
given check reads. It writes two answers to ``$GITHUB_OUTPUT``:

``identical``
    ``true`` when the queue commit's tree is byte-for-byte the PR head's tree
    AND the queue's base commit is an ancestor of the PR head: the base had
    nothing the PR did not already carry. Conformance and pre-commit read the
    tree and nothing else, so on ``true`` they skip.

``image_changed``
    ``true`` when any path the scanned image's vulnerability findings depend
    on may differ between what the PR's checks tested and the queue commit
    (see :func:`is_image_input`). Build & Scan skips on ``false``.

``pr_head``
    The PR head SHA compared against, or empty when none was resolved. A
    later step that trusts a verdict recorded on the head commit reads it
    from here, so the verdict and the compared tree belong to one commit.

What the PR's checks tested: a ``pull_request`` run builds
``refs/pull/N/merge``, the head merged with the base *as it was then*, a base
commit this script cannot know. Comparing the queue commit with the bare head
is not enough on its own: a base change that was present when the PR ran and
was reverted before the queue entry leaves the queue tree equal to the head's
while it differs from the tree that was tested. So the paths that may differ
are the head-to-queue diff PLUS every path any base commit touched since the
PR forked (``git log fork..base``), reverted or not. When the base is an
ancestor of the head that second set is empty and the tested tree was the
head's own, which is the only case ``identical`` may be ``true``.

Where the PR head comes from: a queue branch is named
``refs/heads/gh-readonly-queue/<base>/pr-<number>-<sha>``, and that SHA is the
commit the entry was built ON (the queue commit's parent), not the PR head.
Reading it as the head compared the queue commit with its own base, so
``identical`` was never true (FND-4344). The head is fetched instead from
``refs/pull/<number>/head``, which needs only ``contents: read``. A push to
the PR removes it from the queue, so during an entry's run that ref is the
head that was queued; if it moved anyway, its tree no longer matches and
every check runs. When a group batches several PRs the name carries the LAST
one, and the queue commit then also holds the PRs queued ahead of it, which
shows up as a difference.

**Fails safe.** Any event other than ``merge_group``, a branch name that does
not parse, a base SHA that is missing, a PR head ref that cannot be fetched, a
fork point beyond the fetched history, or a git command that fails or stalls,
yields ``identical=false`` and ``image_changed=true``: every check runs,
exactly as it did before this script existed. A wrong ``true`` here would
green a required check on a tree nothing checked, so the only direction an
error may push is towards more work.

Extracted from inline shell per docs/standards/ci.md (no branching logic in
workflow ``run:`` blocks); unit-tested in tests/test_queue_tree_diff.py. The
classification of every queue check, and why each is or is not base-sensitive,
is in docs/standards/ci.md ("What a merge-queue entry re-runs").

Environment:
    EVENT_NAME      ``github.event_name``.
    QUEUE_HEAD_REF  ``github.event.merge_group.head_ref``.
    QUEUE_SHA       ``github.sha``: the queue commit, already checked out
                    (depth 1) in the working directory.
    QUEUE_BASE_SHA  ``github.event.merge_group.base_sha``.
    GITHUB_OUTPUT   the step output file.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

GitFn = Callable[[list[str]], str]

MERGE_GROUP = "merge_group"

#: A stalled fetch must fail over to "run everything" well inside the job's
#: five-minute limit, not consume it.
GIT_TIMEOUT_S = 90

#: History fetched behind the PR head and the base to find where they forked.
#: A fork older than this reads as "unknown" and every check runs.
FORK_SEARCH_DEPTH = 500

_SHA_RE = re.compile(r"[0-9a-f]{40}")

_QUEUE_REF_RE = re.compile(
    r"^refs/heads/gh-readonly-queue/.+/pr-(?P<number>[0-9]+)-(?P<sha>[0-9a-f]{40})$"
)

#: Where the fetched PR head is stored in the checkout, so it is resolved by
#: name rather than read from ``FETCH_HEAD``, which lists every fetched tip.
PR_HEAD_LOCAL_REF = "refs/queue-diff/pr-head"

#: Basenames, matched case-insensitively, whose change can change what Trivy or
#: Endor find in the image. Vulnerability findings come from the OS packages
#: the base image and Dockerfile install, and from the package metadata of what
#: the lock and manifests resolve; Python source never adds one. So the list
#: is the Dockerfile and what it reads to install things: dependency manifests
#: and locks for every ecosystem a connector image is seen to carry, vendored
#: binaries, and shell scripts (the usual shape of a Dockerfile `RUN` that
#: downloads a driver). `atlan.yaml` names the Dockerfile to build.
IMAGE_INPUT_BASENAMES: tuple[str, ...] = (
    "dockerfile",
    "dockerfile.*",
    "*.dockerfile",
    "containerfile",
    ".dockerignore",
    "atlan.yaml",
    "uv.lock",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements*.txt",
    "requirements*.in",
    "constraints*.txt",
    "poetry.lock",
    "pdm.lock",
    "pipfile",
    "pipfile.lock",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.lockfile",
    "*.jar",
    "*.war",
    "*.whl",
    "*.sh",
)

#: Path prefixes whose change can change the Security Gate's verdict without
#: changing the image: the caller's allowlist.
IMAGE_INPUT_PREFIXES: tuple[str, ...] = (".security/",)


@dataclass(frozen=True)
class Decision:
    """What one queue entry has to re-check, and why."""

    identical: bool
    image_changed: bool
    reason: str
    pr_head: str = ""


def _run_everything(reason: str, pr_head: str = "") -> Decision:
    """Every check runs: the answer for anything that is not a queue entry,
    and for any queue entry this script could not reason about."""
    return Decision(identical=False, image_changed=True, reason=reason, pr_head=pr_head)


def parse_pr_number(head_ref: str) -> int | None:
    """The PR number a queue branch was built for, or None.

    The whole name must parse, SHA included, even though the SHA (the
    entry's parent, not the PR head) is not used: anything else is not a
    queue branch this script knows."""
    match = _QUEUE_REF_RE.match(head_ref.strip())
    return int(match.group("number")) if match else None


def is_image_input(path: str) -> bool:
    """True when a change to ``path`` can change the image scan's verdict."""
    if any(path.startswith(prefix) for prefix in IMAGE_INPUT_PREFIXES):
        return True
    basename = path.rsplit("/", 1)[-1].lower()
    return any(
        fnmatch.fnmatchcase(basename, pattern) for pattern in IMAGE_INPUT_BASENAMES
    )


def _run_git(args: list[str]) -> str:
    """Run git and return stdout; raises CalledProcessError on failure."""
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    ).stdout


def decide(
    event_name: str,
    head_ref: str,
    queue_sha: str,
    base_sha: str = "",
    git: GitFn = _run_git,
) -> Decision:
    """Compare the queue commit with what the PR's checks could have tested."""
    if event_name != MERGE_GROUP:
        return _run_everything(f"event is {event_name!r}, not a merge-queue entry")
    pr_number = parse_pr_number(head_ref)
    if pr_number is None:
        return _run_everything(f"queue ref {head_ref!r} does not name a PR")
    if not _SHA_RE.fullmatch(queue_sha):
        return _run_everything(f"queue sha {queue_sha!r} is not a full commit sha")
    if not _SHA_RE.fullmatch(base_sha):
        return _run_everything(f"queue base sha {base_sha!r} is not a full commit sha")
    pr_head = ""
    try:
        # Commits and trees only (no blobs): enough history behind both tips
        # to find their fork point, and to list what the base touched since.
        git(
            [
                "fetch",
                "--no-tags",
                f"--depth={FORK_SEARCH_DEPTH}",
                "--filter=blob:none",
                "origin",
                f"+refs/pull/{pr_number}/head:{PR_HEAD_LOCAL_REF}",
                base_sha,
            ]
        )
        pr_head = git(["rev-parse", "--verify", PR_HEAD_LOCAL_REF]).strip()
        if not _SHA_RE.fullmatch(pr_head):
            return _run_everything(
                f"PR #{pr_number} head resolved to {pr_head!r}, not a commit sha"
            )
        pr_tree = git(["rev-parse", f"{pr_head}^{{tree}}"]).strip()
        queue_tree = git(["rev-parse", f"{queue_sha}^{{tree}}"]).strip()
        changed_raw = git(
            ["diff", "--no-renames", "--name-only", "-z", pr_head, queue_sha]
        )
        fork = git(["merge-base", pr_head, base_sha]).strip()
        if not _SHA_RE.fullmatch(fork):
            return _run_everything(
                f"no fork point between PR head {pr_head} and base {base_sha} "
                f"within {FORK_SEARCH_DEPTH} commits",
                pr_head,
            )
        base_raw = git(
            [
                "log",
                "--no-renames",
                "--format=",
                "--name-only",
                "-z",
                f"{fork}..{base_sha}",
            ]
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return _run_everything(
            f"git failed comparing {pr_head} with {queue_sha}: {exc}"
        )

    head_diff = {path for path in changed_raw.split("\0") if path.strip()}
    base_touched = {path.strip() for path in base_raw.split("\0") if path.strip()}
    changed = sorted(head_diff | base_touched)
    if pr_tree and pr_tree == queue_tree and fork == base_sha:
        return Decision(
            identical=True,
            image_changed=False,
            reason=(
                f"queue commit tree equals PR head {pr_head} tree, and the head "
                f"already contains base {base_sha}"
            ),
            pr_head=pr_head,
        )
    if not changed:
        # Different tree ids but an empty diff cannot happen for two commits
        # git just resolved; treat it as "could not tell".
        return _run_everything(
            f"trees differ but git reported no changed paths vs {pr_head}",
            pr_head,
        )
    image_inputs = [path for path in changed if is_image_input(path)]
    shown = ", ".join(image_inputs[:10]) + (" ..." if len(image_inputs) > 10 else "")
    return Decision(
        identical=False,
        image_changed=bool(image_inputs),
        reason=(
            f"{len(changed)} path(s) may differ from what PR head {pr_head} "
            f"was tested as ({len(base_touched)} touched by the base since the "
            "fork); "
            + (
                f"image inputs among them: {shown}"
                if image_inputs
                else "none is an image input"
            )
        ),
        pr_head=pr_head,
    )


def _write_outputs(decision: Decision, output_path: str | None) -> None:
    lines = (
        f"identical={'true' if decision.identical else 'false'}\n"
        f"image_changed={'true' if decision.image_changed else 'false'}\n"
        f"pr_head={decision.pr_head}\n"
    )
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(lines)
    sys.stdout.write(lines)


def main(env: dict[str, str] | None = None, git: GitFn = _run_git) -> int:
    env = dict(os.environ) if env is None else env
    decision = decide(
        env.get("EVENT_NAME", ""),
        env.get("QUEUE_HEAD_REF", ""),
        env.get("QUEUE_SHA", ""),
        env.get("QUEUE_BASE_SHA", "").strip(),
        git=git,
    )
    print(f"::notice title=Merge-queue re-check::{decision.reason}")
    _write_outputs(decision, env.get("GITHUB_OUTPUT"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
