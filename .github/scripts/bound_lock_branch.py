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

2. **Two uv projects, with different exempt sets.** ``packages/conformance`` is a
   separate uv project with its own lock, and unlike the root project it resolves
   ``atlan-application-sdk`` from PyPI. Its exempt set therefore has to carry the
   SDK *and* pyatlan, for the reason recorded in the preset's
   ``lockFileMaintenance`` description: with the SDK exempt but pyatlan not, a
   bounded resolve does not fail — it silently backtracks to an older SDK.

3. **One commit, not three.** Each push to the branch re-fires the PR's entire
   required-check suite. Bounding every lock in a single commit costs one CI wave
   instead of one per file.

4. **The npm lock is bounded too, by a different driver.** The lane rewrites a
   third file, ``packages/conformance/conformance/package-lock.json``, and npm
   can express none of the per-package retention ceilings the uv bound depends
   on — so ``renovate_npm_lock_bounded`` gates the whole file instead: take the
   bounded resolve, or restore the base branch's lock verbatim. FND-380. It rides
   in the same commit for the same reason the two uv locks do.


Fail-closed, like the driver it wraps. If any project's bound cannot be applied,
this exits non-zero having committed nothing — a red check on the PR, which the
auto-approve gate then declines to approve. A control whose failure mode is a log
line is not a control (FND-367).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import renovate_npm_lock_bounded as npm_bounded
import renovate_uv_lock_bounded as bounded

COMMIT_MESSAGE = "chore(deps): bound the refreshed locks to the release-age window"


@dataclass(frozen=True)
class Project:
    """A uv project in this repo whose lock the refresh lane rewrites."""

    directory: str
    exempt: tuple[str, ...] = field(default_factory=tuple)


# The repo's uv projects and their exempt sets. Hard-coded rather than passed in
# because these are facts about application-sdk's own layout, not policy a caller
# should be choosing — and keeping them here keeps each exemption next to the
# reason it exists.
PROJECTS: tuple[Project, ...] = (
    # Root. The SDK does not consume itself, and atlan-application-sdk-conformance
    # is path-sourced from packages/conformance via [tool.uv.sources], so it never
    # resolves from PyPI and the bound never sees it. pyatlan is exempt because it
    # is Atlan-published: a week's hold on pyatlan is a week's hold on the SDK,
    # whose own `pyatlan>=N,<N+1` cap is what gates a pyatlan major reaching the
    # fleet at all.
    Project(directory=".", exempt=("pyatlan",)),
    # The conformance package resolves atlan-application-sdk from PyPI (it declares
    # it as a test extra), so both first-party names must be admitted. Dropping
    # pyatlan here would reproduce the silent-backtrack failure exactly: SDK 3.28.0
    # requires pyatlan>=10, so a bound that hides a fresh pyatlan 10 makes the
    # newest SDK unreachable and uv resolves an older one without erroring.
    Project(
        directory="packages/conformance",
        exempt=("atlan-application-sdk", "pyatlan"),
    ),
)

# The one npm project the refresh lane rewrites: dev-only devDependencies for the
# remediation programs, never bundled in the published wheel. Its bound is a
# different mechanism from the uv projects' (see renovate_npm_lock_bounded), so it
# is declared separately rather than squeezed into Project.
NPM_PROJECT = "packages/conformance/conformance"


def bound_project(project: Project, window: str, baseline_ref: str, root: Path) -> int:
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
    return bounded.main(argv)


def bound_npm(window: str, baseline_ref: str, root: Path) -> int:
    """Run the npm driver over the one npm project. Returns its exit code.

    Same window as the uv projects deliberately — see the driver's docstring for
    what that leaves on the table against the org checker's own threshold, and why
    closing that gap is the checker's change to make rather than a second window
    here.
    """
    return npm_bounded.main(
        [
            "--window",
            window,
            "--baseline-ref",
            baseline_ref,
            "--project-dir",
            str(root / NPM_PROJECT),
        ]
    )


def stage_and_commit(root: Path, paths: list[str]) -> bool:
    """Stage the bound outputs and commit iff something changed.

    Returns True when a commit was made. Stages explicit paths only, so nothing
    incidental in the working tree (a uv cache, a stray artefact) can ride along
    into a branch that auto-merges.
    """
    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"], cwd=root, check=True
    )
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "github-actions[bot]@users.noreply.github.com",
        ],
        cwd=root,
        check=True,
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

    subprocess.run(["git", "commit", "-m", COMMIT_MESSAGE], cwd=root, check=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window",
        required=True,
        help="Release-age bound as an ISO 8601 duration, e.g. P7D. Match the "
        "fleet's: a repo-specific window reintroduces the SDK/connector "
        "dependency-set divergence this exists to remove.",
    )
    parser.add_argument(
        "--baseline-ref",
        required=True,
        help="Git ref whose locks are the pre-refresh baseline — the PR's base "
        "branch, e.g. origin/main. Required rather than defaulted: this runs "
        "after Renovate has committed the refresh, so the driver's HEAD default "
        "would silently make the bound a no-op.",
    )
    args = parser.parse_args(argv)

    root = Path.cwd()
    for project in PROJECTS:
        code = bound_project(project, args.window, args.baseline_ref, root)
        if code != 0:
            print(
                f"Bound failed for {project.directory!r}; committing nothing so "
                "the unbounded lock cannot merge.",
                file=sys.stderr,
            )
            return code

    code = bound_npm(args.window, args.baseline_ref, root)
    if code != 0:
        print(
            f"npm bound failed for {NPM_PROJECT!r}; committing nothing so the "
            "unbounded locks cannot merge.",
            file=sys.stderr,
        )
        return code

    paths = [f"{p.directory}/uv.lock".removeprefix("./") for p in PROJECTS]
    paths.append(f"{NPM_PROJECT}/{npm_bounded.LOCKFILE}")
    stage_and_commit(root, paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
