#!/usr/bin/env python3
"""Regenerate the conformance relationship table on a Renovate pyatlan move.

The conformance package commits ``relationship_directions.json``, built from
the installed pyatlan's models and pinned to the locked pyatlan by a drift test
(``packages/conformance/tests/test_relationship_direction.py``). Renovate moves
pyatlan in the root ``uv.lock`` (its ``renovate/pyatlan`` floor lane, or any
other bump that relocks it) but cannot regenerate the table: Mend cannot run
``postUpgradeTasks`` here (see ``lockFileMaintenance`` in renovate.json).
Left alone, such a PR stays red on that drift test until someone regenerates
by hand.

``packages/conformance`` is a member of the root uv workspace (FND-4671), so
the root ``uv.lock`` is the lock the table is built from. This lane used to
also relock a second, conformance-only ``uv.lock`` to every SDK dependency bump
(FND-4439, FND-5018); with that lock gone, the table is all that is left to
follow.

What it does: if the root lock's pyatlan differs from the base branch's,
regenerate the table with ``bound_lock_branch.regenerate_relationship_directions``.
Otherwise nothing.

Two jobs, run by renovate-branch-fixup.yaml as for renovate-lock-cooldown.yaml:
``--no-commit`` regenerates (it installs and imports the new pyatlan) holding
only a read-only token, ``--stage-to`` copies OUTPUT_PATHS into one folder at
their repo paths for the artifact upload, and ``--apply-from`` copies exactly
OUTPUT_PATHS from that artifact and commits them in the job that holds the push
token. Idempotent: an unchanged table commits nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bound_lock_branch as lanes

COMMIT_MESSAGE = "chore(deps): regenerate the relationship table for the locked pyatlan"

# Every file this script may write, and so every file the publishing job may
# commit. The workflow uploads exactly these, staged by --stage-to. The root
# uv.lock is Renovate's own commit, never ours.
OUTPUT_PATHS: tuple[str, ...] = (lanes.RELATIONSHIP_DIRECTIONS,)


def sync(baseline_ref: str, root: Path) -> int:
    """Regenerate the table iff pyatlan moved. Returns a process exit code."""
    if not lanes.pyatlan_moved(baseline_ref, root):
        print(
            f"pyatlan in {lanes.PYATLAN_LOCK} matches {baseline_ref}; table left as is."
        )
        return 0
    code = lanes.regenerate_relationship_directions(root)
    if code != 0:
        print(
            "Regenerating relationship_directions.json failed; committing "
            "nothing so the lock cannot merge without it.",
            file=sys.stderr,
        )
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-ref",
        help="The PR's base branch (e.g. origin/main): the table is regenerated "
        "only when the root lock's pyatlan differs from this ref's.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--no-commit",
        action="store_true",
        help="Regenerate, leaving the outputs for another job to commit.",
    )
    mode.add_argument(
        "--stage-to",
        type=Path,
        metavar="DIR",
        help="Copy OUTPUT_PATHS into DIR at their repo paths, for the artifact upload.",
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
    if args.stage_to is not None:
        lanes.stage_outputs(root, args.stage_to, OUTPUT_PATHS)
        return 0

    if args.apply_from is not None:
        lanes.apply_outputs(args.apply_from, root, OUTPUT_PATHS)
        lanes.stage_and_commit(root, list(OUTPUT_PATHS), COMMIT_MESSAGE)
        return 0

    if not args.baseline_ref:
        parser.error("--baseline-ref is required with --no-commit")
    return sync(args.baseline_ref, root)


if __name__ == "__main__":
    sys.exit(main())
