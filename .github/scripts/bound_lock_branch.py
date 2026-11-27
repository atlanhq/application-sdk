#!/usr/bin/env python3
"""Apply the release-age bound to a Renovate lock-refresh branch in THIS repo.

application-sdk is the one repo the fleet's cooldown cannot reach (FND-376).
The fleet bound (FND-359) runs as a Renovate ``postUpgradeTasks`` command, and
``allowedCommands`` is an admin-only option — so it only exists on the
self-hosted runner. application-sdk's dependency PRs come from Mend, which has
no such hook, leaving the repo that *publishes the wheel ~100 repos install*
as the only one still adopting minutes-old transitive releases unattended.

This closes that gap without changing where the PR comes from: Mend still raises
``renovate/lock-file-maintenance`` exactly as it does today, and a workflow
triggered by Mend's push corrects the lock in place before the PR merges. The
PR stays authored by ``renovate[bot]`` and ``renovate/artifacts`` stays
Renovate's own status, so both conditions in ``renovate-auto-approve-reusable.yml``
hold unchanged and no fleet-wide gate has to be widened for one repo.

What runs here that does not run in the fleet
---------------------------------------------
The bound itself is the shipped, tested driver — ``renovate_uv_lock_bounded``,
unchanged in behaviour. This script is the part that is specific to running
*after* the refresh has been committed rather than before it, and to this repo's
shape:

1. **The baseline is the base branch, not HEAD.** Under ``postUpgradeTasks`` the
   refresh is still uncommitted, so ``HEAD`` is the pre-refresh lock. Here
   Renovate has already committed it, so ``HEAD`` *is* the unbounded lock —
   deriving retention ceilings from that would pin every package to the release
   Renovate pulled seconds ago and neutralise the window completely. Passing the
   base branch also makes "no rollback against what main ships" structural: the
   driver's rollback gate then compares against main by construction.

2. **One uv project.** ``packages/conformance`` is a member of the root uv
   workspace (FND-4671), so the root ``uv.lock`` covers it and the bound runs
   once. The workspace never resolves ``atlan-application-sdk`` from PyPI (it is
   the workspace root), so there is no registry version for the bound to hold or
   move. Before FND-4671 the package carried a second lock, bounded separately.

3. **One commit, not two.** Each push to the branch re-fires the PR's entire
   required-check suite. The bounded lock and the regenerated table ride in a
   single commit, one CI wave instead of one per file.

4. **Range-bump branches too (FND-5472).** When Renovate rewrites a range in
   ``pyproject.toml`` (a major, or the non-critical group), its own relock
   takes the newest release the new range admits, which for a package that
   publishes faster than the window is always too young: ``checks/dep-cooldown``
   fails and the PR can never merge (fastmcp 4 sat 83 days that way).
   ``--no-upgrade`` bounds those branches without turning them into a lock
   refresh: the driver starts from the base branch's lock and drops
   ``--upgrade``, so every package the new manifest still admits stays where
   main has it and only the bumped package's closure resolves, inside the
   window. Renovate's own pick already passed its release-age rule, so the
   floor it wrote is satisfiable; one younger than the window is a deliberate
   floor, which the driver admits and reports.

5. **A pyatlan move regenerates the table derived from it.** The conformance
   package commits ``relationship_directions.json``, built from the installed
   pyatlan's models and pinned to it by a drift test. A refresh that moves
   pyatlan without regenerating it is a red PR nobody on the lane can fix, so
   when the BOUNDED lock's pyatlan differs from the baseline's, the table is
   regenerated and rides in the same commit. After the bound, not before: the
   bound can roll pyatlan back, and a table built for a version that never
   merges is as stale as one never rebuilt.

Two halves, two jobs
--------------------
Regenerating the table installs and imports the freshly-resolved pyatlan. That
must not happen in a job holding the push-capable App token, so the workflow
splits (renovate-branch-fixup.yaml): ``--no-commit`` bounds and regenerates in
a job with a read-only token, ``--stage-to`` copies ``OUTPUT_PATHS`` into one
folder at their repo paths for the artifact upload, and ``--apply-from`` copies
exactly ``OUTPUT_PATHS`` out of that artifact and commits them in the job that
pushes.
The second half runs no resolver and installs nothing.

Fail-closed, like the driver it wraps. If any project's bound cannot be applied,
this exits non-zero having committed nothing — a red check on the PR, which the
auto-approve gate then declines to approve. A control whose failure mode is a log
line is not a control (FND-367).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import renovate_uv_lock_bounded as bounded

COMMIT_MESSAGE = "chore(deps): bound the refreshed locks to the release-age window"

#: The author of every commit the fix-up lanes push onto a Renovate branch. Its
#: email must be in the fleet preset's ``gitIgnoredAuthors``, together with the
#: committer that sign-bot-commits sets, or Renovate reads the branch as
#: human-edited and stops rebasing it (FND-5471). test_renovate_branch_fixup.py
#: pins both.
BOT_AUTHOR_NAME = "github-actions[bot]"
BOT_AUTHOR_EMAIL = "github-actions[bot]@users.noreply.github.com"


@dataclass(frozen=True)
class Project:
    """A uv project in this repo whose lock the refresh lane rewrites."""

    directory: str
    exempt: tuple[str, ...] = field(default_factory=tuple)
    hold: tuple[str, ...] = field(default_factory=tuple)


# The repo's uv projects and their exempt sets. Hard-coded rather than passed in
# because these are facts about application-sdk's own layout, not policy a caller
# should be choosing — and keeping them here keeps each exemption next to the
# reason it exists.
PROJECTS: tuple[Project, ...] = (
    # The workspace root, whose lock also covers packages/conformance
    # (FND-4671). The SDK does not consume itself, and
    # atlan-application-sdk-conformance is a workspace member, so neither
    # resolves from PyPI and the bound never sees them. pyatlan is exempt
    # because it is Atlan-published: a week's hold on pyatlan is a week's hold
    # on the SDK, whose own `pyatlan>=N,<N+1` cap is what gates a pyatlan major
    # reaching the fleet at all.
    Project(directory=".", exempt=("pyatlan",)),
)

# The lock the relationship table is generated from: the workspace lock, which
# `gen-relationship-directions` runs against from packages/conformance.
PYATLAN_LOCK = "uv.lock"
RELATIONSHIP_DIRECTIONS = (
    "packages/conformance/conformance/data/relationship_directions.json"
)

# Every file this script may write, and so every file the publishing job may
# commit. The workflow's artifact upload is pinned to this list by a drift test.
OUTPUT_PATHS: tuple[str, ...] = (
    *(f"{p.directory}/uv.lock".removeprefix("./") for p in PROJECTS),
    RELATIONSHIP_DIRECTIONS,
)


def bound_project(
    project: Project,
    window: str,
    baseline_ref: str,
    root: Path,
    *,
    no_upgrade: bool = False,
) -> int:
    """Run the shipped driver over one project. Returns its exit code."""
    argv = [
        "--window",
        window,
        "--baseline-ref",
        baseline_ref,
        "--project-dir",
        str(root / project.directory),
        # This script owns the commit, so a bound that admits nothing is a
        # net-empty PR in the usual case (byte-identical to the baseline), not
        # the silent substitution it would be under postUpgradeTasks. Without
        # this the driver fails, nothing is pushed, and Renovate's unbounded
        # lock stays on the branch — observed on #3290.
        "--caller-owns-commit",
    ]
    for name in project.exempt:
        argv += ["--exempt", name]
    for name in project.hold:
        argv += ["--hold", name]
    if no_upgrade:
        argv.append("--no-upgrade")
    return bounded.main(argv)


def pyatlan_version(lock_text: str) -> str | None:
    """The locked pyatlan version, or None when the lock does not carry it."""
    for package in tomllib.loads(lock_text).get("package", []):
        if package.get("name") == "pyatlan":
            return package.get("version")
    return None


def pyatlan_moved(baseline_ref: str, root: Path) -> bool:
    """Whether the bounded lock's pyatlan differs from the baseline's.

    An unreadable lock raises rather than answering False: a parse failure read
    as "unchanged" would skip the regeneration silently.
    """
    baseline = subprocess.run(
        ["git", "show", f"{baseline_ref}:{PYATLAN_LOCK}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    current = (root / PYATLAN_LOCK).read_text(encoding="utf-8")
    return pyatlan_version(baseline) != pyatlan_version(current)


def regenerate_relationship_directions(root: Path) -> int:
    """Rebuild the relationship table from the bounded lock's pyatlan.

    `--frozen` installs exactly what the bound left behind and never re-locks:
    without it uv is free to rewrite the lock this script has just bounded.
    `--directory packages/conformance` selects that workspace member, whose
    `test` extra brings in the SDK and so pyatlan, from the root lock.
    """
    return subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "--directory",
            "packages/conformance",
            "--extra",
            "test",
            "atlan-application-sdk-conformance",
            "gen-relationship-directions",
        ],
        cwd=root,
    ).returncode


def apply_outputs(
    source: Path, root: Path, paths: tuple[str, ...] = OUTPUT_PATHS
) -> None:
    """Copy ``paths`` (default OUTPUT_PATHS) from an artifact directory into the
    checkout. renovate_pyatlan_table_sync.py reuses this with its own output list.

    Only the declared paths are read, so nothing else in the artifact can reach
    a branch that auto-merges. Every one must be present: `bound` uploads all of
    them on every run, so a missing one means a broken handoff, and skipping it
    would leave Renovate's unbounded lock on the branch behind a green publish.
    A symlink anywhere along the path is refused rather than followed — a
    symlinked parent such as `packages/` would otherwise smuggle in files from
    outside the artifact under allowlisted names. Everything is checked before
    anything is copied.
    """
    for path in paths:
        src = source
        for part in Path(path).parts:
            src = src / part
            if src.is_symlink():
                raise ValueError(f"refusing symlinked artifact entry: {path}")
        if not src.is_file():
            raise FileNotFoundError(f"missing artifact output: {path}")
    for path in paths:
        shutil.copyfile(source / path, root / path)


def stage_outputs(
    root: Path, dest: Path, paths: tuple[str, ...] = OUTPUT_PATHS
) -> None:
    """Copy ``paths`` from the checkout into ``dest`` at their repo paths.

    renovate-branch-fixup.yaml uploads ``dest`` as the artifact's one folder,
    so the artifact holds every output at its repo path, e.g.
    ``packages/conformance/conformance/data/relationship_directions.json`` and
    not just ``relationship_directions.json``. renovate_pyatlan_table_sync.py
    reuses this with its own output list. Given several file paths, upload-artifact roots the artifact at their least
    common ancestor and drops that prefix, which ``apply_outputs`` (correctly)
    reads as a missing output (FND-5015). One folder has no such rule to trip.

    Same guards as ``apply_outputs``, in the other direction: every path must
    exist, a symlink along a path is refused rather than followed, ``dest``
    must be empty so nothing stale rides along, and everything is checked
    before anything is copied.
    """
    for path in paths:
        src = root
        for part in Path(path).parts:
            src = src / part
            if src.is_symlink():
                raise ValueError(f"refusing symlinked output: {path}")
        if not src.is_file():
            raise FileNotFoundError(f"missing output to stage: {path}")
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"staging folder is not empty: {dest}")
    for path in paths:
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / path, target)


def stage_and_commit(
    root: Path, paths: list[str], message: str = COMMIT_MESSAGE
) -> bool:
    """Stage the bound outputs and commit iff something changed.

    Returns True when a commit was made. Stages explicit paths only, so nothing
    incidental in the working tree (a uv cache, a stray artefact) can ride along
    into a branch that auto-merges.
    """
    subprocess.run(
        ["git", "config", "user.name", BOT_AUTHOR_NAME], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", BOT_AUTHOR_EMAIL], cwd=root, check=True
    )
    for path in paths:
        if (root / path).exists():
            subprocess.run(["git", "add", "--", path], cwd=root, check=True)

    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=root, capture_output=True
    )
    if staged.returncode == 0:
        print("Locks were already within the window — nothing to commit.")
        return False

    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window",
        help="Release-age bound as an ISO 8601 duration, e.g. P7D. Match the "
        "fleet's: a repo-specific window reintroduces the SDK/connector "
        "dependency-set divergence this exists to remove.",
    )
    parser.add_argument(
        "--baseline-ref",
        help="Git ref whose locks are the pre-refresh baseline — the PR's base "
        "branch, e.g. origin/main. Required rather than defaulted: this runs "
        "after Renovate has committed the refresh, so the driver's HEAD default "
        "would silently make the bound a no-op.",
    )
    parser.add_argument(
        "--no-upgrade",
        action="store_true",
        help="Bound a Renovate range-bump branch rather than the lock refresh: "
        "relock from the base branch's lock without --upgrade, so only the "
        "bumped package's closure moves, inside the window (FND-5472).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--no-commit",
        action="store_true",
        help="Bound and regenerate, leaving the outputs in the working tree for "
        "another job to commit.",
    )
    mode.add_argument(
        "--stage-to",
        type=Path,
        metavar="DIR",
        help="Skip the bound: copy OUTPUT_PATHS into DIR at their repo paths, "
        "for the artifact upload.",
    )
    mode.add_argument(
        "--apply-from",
        type=Path,
        metavar="DIR",
        help="Skip the bound: copy OUTPUT_PATHS from DIR (a --no-commit run's "
        "artifact) and commit them.",
    )
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.stage_to is not None:
        stage_outputs(root, args.stage_to)
        return 0

    if args.apply_from is not None:
        apply_outputs(args.apply_from, root)
        stage_and_commit(root, list(OUTPUT_PATHS))
        return 0

    # Required for the bound only, so enforced here rather than by argparse.
    if not args.window or not args.baseline_ref:
        parser.error("--window and --baseline-ref are required to bound")

    for project in PROJECTS:
        code = bound_project(
            project,
            args.window,
            args.baseline_ref,
            root,
            no_upgrade=args.no_upgrade,
        )
        if code != 0:
            print(
                f"Bound failed for {project.directory!r}; committing nothing so "
                "the unbounded lock cannot merge.",
                file=sys.stderr,
            )
            return code

    if pyatlan_moved(args.baseline_ref, root):
        code = regenerate_relationship_directions(root)
        if code != 0:
            print(
                "Regenerating relationship_directions.json failed; committing "
                "nothing so the locks cannot merge without it.",
                file=sys.stderr,
            )
            return code

    if not args.no_commit:
        stage_and_commit(root, list(OUTPUT_PATHS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
