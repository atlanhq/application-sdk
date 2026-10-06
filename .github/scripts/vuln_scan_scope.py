#!/usr/bin/env python3
"""Decide whether a Build & Scan run on a PR or queue entry scans at all (FND-3328).

The vulnerability scan used to build and scan an image on every
``pull_request`` push and every ``merge_group`` entry. Since FND-3327 the only
image an app with a release flow ships is the release image, so scanning an
image per PR checks something that never ships. The scan now runs on the
bump-version PR, the last gate before a release, and that image is the one
promoted at release (see ``release_candidate.py``).

This script answers one question for ``build-and-scan.yaml``: ``scan=true`` or
``scan=false``. On ``false`` the ``Build Image`` and ``Security Gate`` jobs
skip, and a skipped job files its check as ``skipped``, which a required
context counts as a pass. That is why the decision lives inside the reusable
workflow rather than in the caller's ``on:`` filter: a workflow that never
starts files no check at all, and the required ``scan / Security Gate`` would
then block every PR.

Rules, in order:

* any event other than ``pull_request`` / ``merge_group`` scans (the job that
  runs this is not even started for them; listed for completeness);
* ``SCAN_EVERY_PR=true`` (the caller's opt-back-in) scans;
* a repo with **no release flow** scans every PR and queue entry, exactly as
  before. Without a bump PR nothing else would ever gate its image, so the
  posture change Security signed off on does not apply to it. "Has a release
  flow" means a workflow on the **base** branch calls
  ``release-version-bump.yaml``: reading the base, not the PR, means a PR cannot
  opt itself out by adding such a file;
* a ``pull_request`` from a ``bump-version*`` branch scans;
* everything else (ordinary PRs, every queue entry) skips.

**Fails safe.** A workflows directory that is missing or unreadable reads as
"no release flow", so the run scans. The only direction an error may push is
towards more work.

Extracted per docs/standards/ci.md; unit-tested in
tests/test_vuln_scan_scope.py.

Environment:
    EVENT_NAME      ``github.event_name``.
    HEAD_REF        ``github.head_ref`` (empty outside ``pull_request``).
    SCAN_EVERY_PR   ``inputs.scan_every_pr`` (``true`` / ``false``).
    WORKFLOWS_DIR   the base branch's ``.github/workflows``, checked out.
    GITHUB_OUTPUT   the step output file.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: The branch prefix the release flow pushes its bump PR from
#: (``bump-version-<target>``). Same test release.yaml uses to exclude it.
BUMP_BRANCH_PREFIX = "bump-version"

#: The reusable workflow a release-flow repo's ``release.yaml`` calls.
RELEASE_FLOW_MARKER = "release-version-bump.yaml"

SCOPED_EVENTS = ("pull_request", "merge_group")


@dataclass(frozen=True)
class Scope:
    scan: bool
    reason: str


def has_release_flow(workflows_dir: Path) -> bool:
    """Return True when any workflow in *workflows_dir* calls the bump workflow."""
    try:
        files = sorted(workflows_dir.iterdir())
    except OSError:
        return False
    for path in files:
        if path.suffix not in (".yml", ".yaml"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if RELEASE_FLOW_MARKER in text:
            return True
    return False


def decide(
    event_name: str, head_ref: str, scan_every_pr: bool, release_flow: bool
) -> Scope:
    if event_name not in SCOPED_EVENTS:
        return Scope(True, f"event {event_name!r} is always scanned")
    if scan_every_pr:
        return Scope(True, "caller set scan_every_pr")
    if not release_flow:
        return Scope(
            True,
            f"no workflow on the base branch calls {RELEASE_FLOW_MARKER}: "
            "without a bump PR every PR is scanned",
        )
    if event_name == "pull_request" and head_ref.startswith(BUMP_BRANCH_PREFIX):
        return Scope(True, f"bump-version PR ({head_ref}): the release gate")
    if event_name == "merge_group":
        return Scope(
            False,
            "merge-queue entry in a release-flow repo: the bump PR is the scan gate",
        )
    return Scope(
        False,
        "ordinary PR in a release-flow repo: the bump PR is the scan gate, and "
        "the image it scans is the one the release promotes",
    )


def main() -> int:
    env = os.environ
    workflows_dir = Path(env.get("WORKFLOWS_DIR", ".github/workflows"))
    scope = decide(
        env.get("EVENT_NAME", ""),
        env.get("HEAD_REF", ""),
        env.get("SCAN_EVERY_PR", "").strip().lower() == "true",
        has_release_flow(workflows_dir),
    )
    verdict = "true" if scope.scan else "false"
    print(f"scan={verdict}: {scope.reason}", flush=True)
    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"scan={verdict}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
