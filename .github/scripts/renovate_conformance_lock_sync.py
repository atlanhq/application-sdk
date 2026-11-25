#!/usr/bin/env python3
"""Carry a Renovate dependency bump into the conformance lock (FND-4439, FND-5018).

``packages/conformance/uv.lock`` path-sources the SDK and api packages from this
checkout (FND-4256), so it records the dependencies they declare. Renovate
updates their manifests and relocks the ROOT project only; Mend cannot run
``postUpgradeTasks`` here (see ``lockFileMaintenance`` in renovate.json), so it
never touches the conformance lock. Left alone, every such PR fails
``check_conformance_lock.py`` (the SDK Gate) and the scripts tests that read
the committed lock, until someone runs ``(cd packages/conformance && uv lock)``.

What it does, all of it in the tested functions below:

1. Ask ``conformance_wheel_gate`` which dependencies the SDK and api declare
   differently from what the conformance lock recorded: the comparison
   ``check_conformance_lock.py`` and the release gate fail on.
2. Pin each of those, and pyatlan, to the version Renovate locked at the root,
   when the conformance lock lacks it or is behind it. Up-only: never move a
   lock backwards (the lock-refresh lane may have got there first). Pinned to
   Renovate's pick, never "latest", so the root's release-age cooldown carries
   over.
3. If anything drifted or needs a pin, relock with
   ``uv lock --upgrade-package <pkg>==<root version>`` for every pin. A package
   a relock newly added (a new dependency's own dependencies) is pinned to the
   root's version too, for the same reason, relocking again until a relock
   adds nothing new (``relock_until_settled``).
4. Fail if the relocked lock still drifts: committing it would leave the PR red.
5. If the conformance lock's pyatlan now differs from the base branch's,
   regenerate ``relationship_directions.json`` with
   ``bound_lock_branch.regenerate_relationship_directions``.

Two jobs, run by renovate-branch-fixup.yaml as for renovate-lock-cooldown.yaml: ``--no-commit`` resolves and
regenerates (it installs and imports the new pyatlan) holding only a read-only
token, ``--stage-to`` copies OUTPUT_PATHS into one folder at their repo paths
for the artifact upload, and ``--apply-from`` copies exactly OUTPUT_PATHS from
that artifact and commits them in the job that holds the push token.
Idempotent: unchanged outputs commit nothing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bound_lock_branch as lanes
import conformance_wheel_gate as gate

COMMIT_MESSAGE = (
    "chore(deps): sync the conformance lock to the root lock's dependencies"
)

ROOT_LOCK = "uv.lock"
CONFORMANCE_DIR = "packages/conformance"
PYATLAN = "pyatlan"

# Every file this script may write, and so every file the publishing job may
# commit. The workflow uploads exactly these, staged by --stage-to.
OUTPUT_PATHS: tuple[str, ...] = (
    lanes.PYATLAN_LOCK,
    lanes.RELATIONSHIP_DIRECTIONS,
)


def locked_versions(lock_text: str) -> dict[str, str]:
    """``name -> version`` for every versioned package in a uv.lock."""
    out: dict[str, str] = {}
    for package in tomllib.loads(lock_text).get("package", []):
        name, version = package.get("name"), package.get("version")
        if name and version:
            out[name] = version
    return out


def needs_sync(root_version: str | None, conformance_version: str | None) -> bool:
    """Whether the conformance lock's version is behind the root lock's.

    False when the root lock has no version to pin to: the relock then
    resolves that package from the declarations alone.
    """
    if root_version is None:
        return False
    if conformance_version is None:
        return True
    # Imported here, not at the top: --apply-from runs on the publishing job's
    # bare python3 (stdlib only), which must not need packaging installed.
    from packaging.version import Version  # noqa: PLC0415

    return Version(conformance_version) < Version(root_version)


def plan_pins(
    drifted: set[str], root: dict[str, str], conformance: dict[str, str]
) -> dict[str, str]:
    """The ``name -> root version`` pins for the relock (step 2).

    pyatlan is always considered: a floor raised to the version the root lock
    already pins changes no lock, but the conformance lock may still lag. A
    root lock without pyatlan is a broken input, not "nothing to do".
    """
    if PYATLAN not in root:
        raise ValueError(f"{ROOT_LOCK} carries no {PYATLAN} package")
    local = {gate.canonical_name(n) for n in gate.LOCAL_PACKAGES}
    return {
        name: root[name]
        for name in sorted(drifted | {PYATLAN})
        if name not in local and needs_sync(root.get(name), conformance.get(name))
    }


def relock(pins: dict[str, str], root: Path) -> int:
    """Relock the conformance package, moving each pin to exactly its version."""
    argv = ["uv", "lock", "--directory", CONFORMANCE_DIR]
    for name, version in sorted(pins.items()):
        argv += ["--upgrade-package", f"{name}=={version}"]
    return subprocess.run(argv, cwd=root).returncode


def newly_added_pins(
    before: dict[str, str], after: dict[str, str], root: dict[str, str]
) -> dict[str, str]:
    """Packages the relock added that the root lock holds at another version."""
    return {
        name: root[name]
        for name in sorted(set(after) - set(before))
        if name in root and after[name] != root[name]
    }


#: Relocks allowed after the first before the added set must have settled. Each
#: round can only pin packages the previous one newly added, so a real
#: dependency chain settles in one or two; hitting the cap means something is
#: undoing the pins, and committing that lock would be outside the root's picks.
MAX_PIN_ROUNDS = 5


def relock_until_settled(
    pins: dict[str, str],
    before: dict[str, str],
    root_versions: dict[str, str],
    root: Path,
) -> int:
    """Relock with ``pins``, then re-pin newly added packages until none are new.

    A pin can pull in packages of its own, so every relock is re-scanned
    against the lock as it was before the first one, not only the first
    relock's result. Returns a process exit code.
    """
    code = relock(pins, root)
    lock_path = root / lanes.PYATLAN_LOCK
    for round_ in range(MAX_PIN_ROUNDS + 1):
        if code != 0:
            return code
        after = locked_versions(lock_path.read_text("utf-8"))
        added = newly_added_pins(before, after, root_versions)
        fresh = {name: v for name, v in added.items() if pins.get(name) != v}
        if not fresh:
            return 0
        if round_ == MAX_PIN_ROUNDS:
            break
        print(f"Pinning newly added packages to the root's: {fresh}.", flush=True)
        pins = {**pins, **fresh}
        code = relock(pins, root)
    print(
        f"Newly added packages still off the root's versions after "
        f"{MAX_PIN_ROUNDS} re-pins; committing nothing.",
        file=sys.stderr,
    )
    return 1


def sync(baseline_ref: str, root: Path) -> int:
    """Steps 1-5 of the module docstring. Returns a process exit code."""
    lock_path = root / lanes.PYATLAN_LOCK
    root_versions = locked_versions((root / ROOT_LOCK).read_text("utf-8"))
    before = locked_versions(lock_path.read_text("utf-8"))
    problems = gate.lock_drift(lock_path)
    pins = plan_pins(gate.drifted_packages(lock_path), root_versions, before)

    if problems or pins:
        for problem in problems:
            print(f"Drift: {problem}")
        # Flushed so it reads before uv's own output in the job log.
        print(
            f"Relocking {lanes.PYATLAN_LOCK}, pinning {pins or 'nothing'}.", flush=True
        )
        code = relock_until_settled(pins, before, root_versions, root)
        if code != 0:
            print(
                "uv lock failed for the conformance package; committing nothing.",
                file=sys.stderr,
            )
            return code
        remaining = gate.lock_drift(lock_path)
        if remaining:
            for problem in remaining:
                print(f"Still drifted after the relock: {problem}", file=sys.stderr)
            return 1
    else:
        print(f"{lanes.PYATLAN_LOCK} already matches the root lock; left as is.")

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
