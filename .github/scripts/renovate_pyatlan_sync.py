#!/usr/bin/env python3
"""Carry a pyatlan floor bump into the conformance package (FND-4439).

Renovate's ``renovate/pyatlan`` lane raises the SDK's published ``pyatlan>=X``
floor (root ``pyproject.toml`` and the api package's ``pyatlan`` extra) and
relocks the ROOT project. It cannot reach the conformance package's own lock:
``packages/conformance`` does not declare pyatlan, it gets it through
``atlan-application-sdk`` resolved from PyPI, so Renovate never touches that
lock. That lock is the one the conformance environment installs, and the one
``relationship_directions.json`` is built from and pinned to by the drift test
``test_committed_table_matches_pinned_pyatlan``.

Without this step the bump still merges green: the table matches the conformance
lock, which simply stays on the older pyatlan until the next lock refresh. What
it adds is that one PR moves both environments to the same pyatlan, with the
table rebuilt beside them, the same file set as the hand-made precedent (#4564).

What it does, all of it in the tested functions below:

1. Read the pyatlan version Renovate locked at the root.
2. If the conformance lock is already at or above it, do nothing. Up-only: the
   lock-refresh lane moves pyatlan in that lock too (pyatlan is exempt from its
   cooldown), and it may get there first. Never move a lock backwards.
3. Otherwise ``uv lock --upgrade-package pyatlan==<root version>`` in
   ``packages/conformance``, pinned to Renovate's pick rather than "latest".
4. If that moved the conformance lock's pyatlan away from the base branch's,
   regenerate the table with ``bound_lock_branch.regenerate_relationship_directions``.

Two jobs, as in renovate-lock-cooldown.yaml: ``--no-commit`` resolves and
regenerates (it installs and imports the new pyatlan) holding only a read-only
token, and ``--apply-from`` copies exactly OUTPUT_PATHS from that job's artifact
and commits them in the job that holds the push token. Idempotent: unchanged
outputs commit nothing.

Scope: only ``renovate/pyatlan`` (minor/patch/bump). A major bump lives on
``renovate/major-pyatlan`` and is not handled here: the conformance lock takes
pyatlan through the published SDK's ``<N+1`` cap, so it cannot follow a major
until an SDK release raises that cap.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bound_lock_branch as lanes

COMMIT_MESSAGE = (
    "chore(deps): sync the conformance lock and relationship table to the "
    "locked pyatlan"
)

ROOT_LOCK = "uv.lock"
CONFORMANCE_DIR = "packages/conformance"

# Every file this script may write, and so every file the publishing job may
# commit. The workflow's artifact upload is pinned to this list by a test.
OUTPUT_PATHS: tuple[str, ...] = (
    lanes.PYATLAN_LOCK,
    lanes.RELATIONSHIP_DIRECTIONS,
)


def needs_sync(root_version: str | None, conformance_version: str | None) -> bool:
    """Whether the conformance lock's pyatlan is behind the root lock's.

    A root lock without pyatlan is a broken input, not "nothing to do": the
    lane exists because the root lock moved pyatlan, so raise.
    """
    if root_version is None:
        raise ValueError(f"{ROOT_LOCK} carries no pyatlan package")
    if conformance_version is None:
        return True
    # Imported here, not at the top: --apply-from runs on the publishing job's
    # bare python3 (stdlib only), which must not need packaging installed.
    from packaging.version import Version  # noqa: PLC0415

    return Version(conformance_version) < Version(root_version)


def upgrade_conformance_pyatlan(version: str, root: Path) -> int:
    """Move the conformance lock's pyatlan to exactly ``version``."""
    return subprocess.run(
        [
            "uv",
            "lock",
            "--directory",
            CONFORMANCE_DIR,
            "--upgrade-package",
            f"pyatlan=={version}",
        ],
        cwd=root,
    ).returncode


def sync(baseline_ref: str, root: Path) -> int:
    """Steps 1-4 of the module docstring. Returns a process exit code."""
    root_version = lanes.pyatlan_version((root / ROOT_LOCK).read_text("utf-8"))
    conformance_version = lanes.pyatlan_version(
        (root / lanes.PYATLAN_LOCK).read_text("utf-8")
    )
    if needs_sync(root_version, conformance_version):
        print(
            f"Moving {lanes.PYATLAN_LOCK} pyatlan "
            f"{conformance_version} -> {root_version}."
        )
        code = upgrade_conformance_pyatlan(str(root_version), root)
        if code != 0:
            print(
                "uv lock failed for the conformance package; committing nothing.",
                file=sys.stderr,
            )
            return code
    else:
        print(
            f"{lanes.PYATLAN_LOCK} already has pyatlan {conformance_version} "
            f"(root {root_version}); lock left as is."
        )

    if lanes.pyatlan_moved(baseline_ref, root):
        code = lanes.regenerate_relationship_directions(root)
        if code != 0:
            print(
                "Regenerating relationship_directions.json failed; committing "
                "nothing so the lock cannot merge without it.",
                file=sys.stderr,
            )
            return code
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-ref",
        help="The PR's base branch (e.g. origin/main): the table is regenerated "
        "only when the conformance lock's pyatlan differs from this ref's.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--no-commit",
        action="store_true",
        help="Sync and regenerate, leaving the outputs for another job to commit.",
    )
    mode.add_argument(
        "--apply-from",
        type=Path,
        metavar="DIR",
        help="Copy OUTPUT_PATHS from DIR (a --no-commit run's artifact) and "
        "commit them.",
    )
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.apply_from is not None:
        lanes.apply_outputs(args.apply_from, root, OUTPUT_PATHS)
        lanes.stage_and_commit(root, list(OUTPUT_PATHS), COMMIT_MESSAGE)
        return 0

    if not args.baseline_ref:
        parser.error("--baseline-ref is required with --no-commit")
    return sync(args.baseline_ref, root)


if __name__ == "__main__":
    sys.exit(main())
