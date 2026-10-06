#!/usr/bin/env python3
"""Decide what a merge-queue entry still has to re-check (FND-3321).

A queue entry exists to catch what changes when a PR is combined with the
latest base. A check whose inputs are the same on the queue commit as on the
PR head would only repeat a verdict the PR already earned: a PR cannot enter
the queue until its required checks are green on its head. This script works
out, for one ``merge_group`` run, whether the combination changed anything a
given check reads. It writes two answers to ``$GITHUB_OUTPUT``:

``identical``
    ``true`` when the queue commit's tree is byte-for-byte the PR head's tree:
    the base had nothing the PR did not already carry. Conformance and
    pre-commit read the tree and nothing else, so on ``true`` they skip.

``image_changed``
    ``true`` when any path the scanned image's vulnerability findings depend
    on differs between the PR head and the queue commit (see
    :func:`is_image_input`). Build & Scan skips on ``false``.

Why the PR *head* is the reference, not the commit the PR's run checked out:
a ``pull_request`` run builds ``refs/pull/N/merge``, i.e. the head merged with
the base as it was then. Diffing the queue commit against the head can only
report MORE than the difference from that merge ref — base changes the PR run
already saw still appear — so the comparison errs toward re-running.

Where the PR head comes from: a queue branch is named
``refs/heads/gh-readonly-queue/<base>/pr-<number>-<head sha>``. When a group
batches several PRs the name carries the LAST one, and the queue commit then
also holds the PRs queued ahead of it, which shows up as a difference.

**Fails safe.** Any event other than ``merge_group``, a branch name that does
not parse, or a git command that fails, yields ``identical=false`` and
``image_changed=true``: every check runs, exactly as it did before this script
existed. A wrong ``true`` here would green a required check on a tree nothing
checked, so the only direction an error may push is towards more work.

Extracted from inline shell per docs/standards/ci.md (no branching logic in
workflow ``run:`` blocks); unit-tested in tests/test_queue_tree_diff.py. The
classification of every queue check, and why each is or is not base-sensitive,
is in docs/standards/ci.md ("What a merge-queue entry re-runs").

Environment:
    EVENT_NAME      ``github.event_name``.
    QUEUE_HEAD_REF  ``github.event.merge_group.head_ref``.
    QUEUE_SHA       ``github.sha``: the queue commit, already checked out
                    (depth 1) in the working directory.
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

_QUEUE_REF_RE = re.compile(
    r"^refs/heads/gh-readonly-queue/.+/pr-(?P<number>[0-9]+)-(?P<sha>[0-9a-f]{40})$"
)

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


def _run_everything(reason: str) -> Decision:
    """Every check runs: the answer for anything that is not a queue entry,
    and for any queue entry this script could not reason about."""
    return Decision(identical=False, image_changed=True, reason=reason)


def parse_pr_head(head_ref: str) -> str | None:
    """The PR head SHA a queue branch was built from, or None."""
    match = _QUEUE_REF_RE.match(head_ref.strip())
    return match.group("sha") if match else None


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
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def decide(
    event_name: str, head_ref: str, queue_sha: str, git: GitFn = _run_git
) -> Decision:
    """Compare the queue commit with the PR head it was built from."""
    if event_name != MERGE_GROUP:
        return _run_everything(f"event is {event_name!r}, not a merge-queue entry")
    pr_head = parse_pr_head(head_ref)
    if pr_head is None:
        return _run_everything(f"queue ref {head_ref!r} does not name a PR head")
    if not re.fullmatch(r"[0-9a-f]{40}", queue_sha):
        return _run_everything(f"queue sha {queue_sha!r} is not a full commit sha")
    try:
        # Depth 1 is enough: comparing two trees needs the two commits and
        # their trees, never their history.
        git(["fetch", "--no-tags", "--depth=1", "origin", pr_head])
        pr_tree = git(["rev-parse", f"{pr_head}^{{tree}}"]).strip()
        queue_tree = git(["rev-parse", f"{queue_sha}^{{tree}}"]).strip()
        changed_raw = git(
            ["diff", "--no-renames", "--name-only", "-z", pr_head, queue_sha]
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        return _run_everything(
            f"git failed comparing {pr_head} with {queue_sha}: {exc}"
        )

    changed = [path for path in changed_raw.split("\0") if path]
    if pr_tree and pr_tree == queue_tree:
        return Decision(
            identical=True,
            image_changed=False,
            reason=f"queue commit tree equals PR head {pr_head} tree",
        )
    if not changed:
        # Different tree ids but an empty diff cannot happen for two commits
        # git just resolved; treat it as "could not tell".
        return _run_everything(
            f"trees differ but git reported no changed paths vs {pr_head}"
        )
    image_inputs = [path for path in changed if is_image_input(path)]
    shown = ", ".join(image_inputs[:10]) + (" ..." if len(image_inputs) > 10 else "")
    return Decision(
        identical=False,
        image_changed=bool(image_inputs),
        reason=(
            f"{len(changed)} path(s) differ from PR head {pr_head}; "
            + (
                f"image inputs among them: {shown}"
                if image_inputs
                else "none is an image input"
            )
        ),
    )


def _write_outputs(decision: Decision, output_path: str | None) -> None:
    lines = (
        f"identical={'true' if decision.identical else 'false'}\n"
        f"image_changed={'true' if decision.image_changed else 'false'}\n"
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
        git=git,
    )
    print(f"::notice title=Merge-queue re-check::{decision.reason}")
    _write_outputs(decision, env.get("GITHUB_OUTPUT"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
