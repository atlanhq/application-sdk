#!/usr/bin/env python3
"""Apply the release-age bound to ``package-lock.json`` on the refresh lane.

FND-376 bounded the three Python files ``renovate/lock-file-maintenance``
rewrites in this repo. It left the fourth — ``packages/conformance/conformance/
package-lock.json`` — unbounded, which was the whole of the remaining
``checks/dep-cooldown`` failure on that lane (measured on #3355: the branch's
entire net diff was that one file, and the check named ``fast-uri@3.1.6``,
published the same day). FND-380 closes it.

Why this is not the uv driver with a different command
-----------------------------------------------------
``renovate_uv_lock_bounded`` gives every package a *retention ceiling* —
``--exclude-newer-package <name>=<upload-time of the version main pins>`` — so a
bounded resolve can decline to adopt something new without ever rolling anything
back. **npm has no per-package equivalent, and there is no forward-only mode at
all.** Measured against this project on npm 11.19.0, with ``X`` a 7-day cutoff:

===========================================================  ======================
command                                                      versus ``main``
===========================================================  ======================
``npm install --package-lock-only`` (lock present)           byte-identical, no-op
``npm install --package-lock-only --before=X`` (lock present) **no-op** — npm keeps
                                                             a lock that already
                                                             satisfies the ranges
``npm install --package-lock-only --before=X`` (no lock)      4 rolled BACK
``npm update --package-lock-only --before=X`` (lock present)  the same 4 rolled BACK
===========================================================  ======================

The rollbacks were ``fast-uri 3.1.6 -> 3.1.5``, ``hono 4.13.4 -> 4.13.2``,
``jose 6.2.10 -> 6.2.9`` and ``negotiator 1.1.0 -> 1.0.0`` — all four adopted by
``main`` before this bound existed, so all four are inside a 7-day window. At the
``P3D`` this lane actually runs, three of them still are (``negotiator`` has since
aged out and ``hono`` resolves to 4.13.3). Either way it is the mass-rollback
failure FND-359 corrected before merge, and reverting a version adopted because it
fixed something is worse than adopting a fix late.

Note the second row especially: it is the silent one. Leaving the committed lock
in place and adding ``--before`` produces a *clean exit and no change whatsoever*,
so a bound written that way would report success, bound nothing, and look
identical to a lane with nothing to do. The lock must be removed first for the
date bound to mean anything.

So the bound here is all-or-nothing on the whole file
----------------------------------------------------
1. Re-resolve from ``package.json`` alone, with ``--before`` set to the window.
2. Compare the result against the lock ``main`` ships. Any package whose newest
   locked version went *down* is a regression.
3. On any regression, restore ``main``'s lock **verbatim** and take nothing.
   Otherwise take the bounded resolve.

The fallback is byte-for-byte what ``main`` already ships, so this can never roll
a version back — not as a policy that holds if the comparison is right, but
structurally, because the only two things it can ever write are a resolve that
regressed nothing and the file it is comparing against. And it can never adopt
something fresh, because npm did the date filtering and the whole refresh is
declined when any part of it regresses.

What that costs, and why a decline is not a failure
---------------------------------------------------
The coupling is real: one package that ``main`` adopted inside the window holds
the entire npm lock until it ages out. Today that is three of them, so this
declines and the lane's npm diff is empty. It is not a permanent stall — under
this bound ``main`` only ever takes versions that were already ``--window`` old,
so once the pre-bound adoptions age past the window a bounded resolve stops
regressing and the file starts moving again.

A decline is therefore an ordinary outcome, reported and exit 0, not an error. It
has to be: nothing in CI installs this lock (no ``npm ci`` anywhere in the repo),
so unlike the uv side there is no required check whose failure could hold the
branch, and there is nothing for a red to protect. What a decline produces is
exactly the state the branch would be in if the file were in ``ignorePaths`` —
which is the safe state. Errors that are *not* safe — npm unavailable, the
registry unreachable, a lock that cannot be parsed, a manifest that has moved out
from under the baseline — return non-zero and the caller commits nothing, the
same fail-closed-not-fail-partial rule ``bound_lock_branch`` applies to the uv
projects.

Window
------
``P3D``, passed by the caller to match the uv lane rather than the checker. Note
what that means: ``checks/dep-cooldown`` (the ``atlan-security`` App, not a
workflow in this repo) still enforces **7 days** — measured 2026-08-24 on #3365,
which it failed on a 3-day-old release. So an adoption aged between 3 and 7 days
is bounded correctly by policy and still reported by the check. That residual gap
is deliberate and recorded in ``docs/standards/ci.md``; closing it means aligning
the App's threshold to the fleet's 3-day policy (FND-761), not widening the bound
here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import renovate_uv_lock_bounded as uv_bounded

LOCKFILE = "package-lock.json"
MANIFEST = "package.json"

# `npm install` resolving nothing but the tree. `--ignore-scripts` is belt and
# braces — `--package-lock-only` installs nothing, so there is no lifecycle script
# to run — and the other two only quieten output that would otherwise bury the
# resolve's own errors.
NPM_RESOLVE = [
    "npm",
    "install",
    "--package-lock-only",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
]

_SEMVER_RE = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def semver_key(version: str) -> tuple | None:
    """Semver precedence as a sortable tuple, or None when it does not parse.

    Deliberately not ``packaging.Version``, which the uv driver's ``_version_key``
    uses. PEP 440 gets many semver strings right, and two categories dangerously
    wrong. Both measured against packaging 26.3:

    * **Inverted.** ``1.0.0-1`` normalises to ``1.0.0.post1``, which compares
      *greater* than ``1.0.0`` — so a rollback from ``1.0.0`` to the prerelease
      ``1.0.0-1`` would read as an upgrade and pass the gate.
    * **Unparseable.** ``7.0.0-next.5``, ``1.0.0-alpha.beta`` and ``1.2.3-0.3.7``
      are all valid semver and all raise ``InvalidVersion``. ``-next.N`` is an
      ordinary npm release channel, so this is not an exotic case, and the uv
      driver treats "cannot parse" as a regression — which would wedge this file
      into declining every run for as long as one such version was in the tree.

    The ordering implemented here is semver.org §11: release triple first, then a
    release outranking any prerelease of itself, then prerelease identifiers
    compared one by one with numeric ones ordering below alphanumeric ones, and a
    shorter run of identifiers ordering below a longer one that shares its prefix.
    Build metadata is ignored, as semver requires — PEP 440 would read it as a
    local version and rank ``1.0.0+build.1`` above ``1.0.0``.

    Returns None rather than guessing, so the caller can report an unparseable
    version instead of silently dropping the comparison — the same choice
    ``renovate_uv_lock_bounded._version_key`` makes, for the same reason: the
    version nobody can order is the one most worth a human look.
    """
    match = _SEMVER_RE.match(version.strip())
    if not match:
        return None
    major, minor, patch, prerelease = match.groups()
    release = (int(major), int(minor), int(patch))
    if prerelease is None:
        # No prerelease outranks every prerelease of the same release, hence the 1.
        return (release, 1, ())
    identifiers: list[tuple[int, int | str]] = []
    for part in prerelease.split("."):
        # Numeric identifiers compare numerically and rank below alphanumeric
        # ones, so the leading 0/1 carries that class distinction into the tuple.
        identifiers.append((0, int(part)) if part.isdigit() else (1, part))
    return (release, 0, tuple(identifiers))


def package_name(path: str, entry: dict) -> str | None:
    """The package name for one ``packages`` entry, or None for the project root.

    npm keys ``packages`` by *install path* — ``node_modules/@openprose/reactor``,
    ``node_modules/negotiator/node_modules/content-type`` — and only the root
    entry (key ``""``) carries an explicit ``name``. The name is whatever follows
    the last ``node_modules/``, which keeps the ``@scope/`` prefix intact.
    """
    if not path:
        return None
    name = entry.get("name")
    if isinstance(name, str) and name:
        return name
    marker = "node_modules/"
    index = path.rfind(marker)
    return path[index + len(marker) :] if index != -1 else path


def lock_versions(lock_text: str) -> dict[str, set[str]]:
    """``{package name: every version the lock pins it at}``.

    Keyed by NAME and not by install path, because the install path is a resolver
    detail: a package that moves between a hoisted and a nested position changes
    its key without changing its version, and comparing paths would read that as
    one entry vanishing and another appearing. Observed in this project — a
    ``negotiator`` downgrade takes its nested ``content-type`` copy with it, so
    the path set differs between the two resolves while the versions that matter
    are still directly comparable. A name can legitimately appear at several
    versions at once (three separate ``content-type`` copies here), hence a set
    rather than a single version.

    An empty or unparseable document raises, so a truncated lock cannot read as
    "no packages" — which would make every comparison against it vacuous.
    """
    document = json.loads(lock_text)
    packages = document.get("packages")
    if not isinstance(packages, dict):
        raise ValueError(
            "lockfile has no `packages` table, so nothing can be compared; "
            "expected npm lockfileVersion 2 or 3"
        )
    versions: dict[str, set[str]] = {}
    for path, entry in packages.items():
        if not isinstance(entry, dict):
            continue
        name = package_name(path, entry)
        version = entry.get("version")
        if name and isinstance(version, str) and version:
            versions.setdefault(name, set()).add(version)
    return versions


def regressions(
    before: dict[str, set[str]], after: dict[str, set[str]]
) -> dict[str, tuple[str, str]]:
    """``{name: (baseline newest, resolved newest)}`` for anything that went down.

    Compares the NEWEST version each name is pinned at on either side. That is
    the question the gate is actually asking — "does this resolve take away a
    release ``main`` already has?" — and it is the formulation that survives a
    tree reshuffle, where the same versions sit at different install paths.

    A name that disappears entirely is not a regression: it means nothing depends
    on it in the bounded solution any more. Whatever *caused* it to drop out is
    itself a version move, and shows up here under its own name.

    A version that does not parse is reported rather than skipped — an unorderable
    version is exactly the case worth declining over — but only when the name's
    version set actually *moved*. A non-semver pin that both sides share (a git or
    file dependency, say) would otherwise decline every run forever, which is a
    wedge, not a control.
    """
    found: dict[str, tuple[str, str]] = {}
    for name, baseline_versions in before.items():
        resolved_versions = after.get(name)
        if not resolved_versions or baseline_versions == resolved_versions:
            continue
        newest_before = newest(baseline_versions)
        newest_after = newest(resolved_versions)
        if newest_before is None or newest_after is None:
            found[name] = (
                ", ".join(sorted(baseline_versions)),
                ", ".join(sorted(resolved_versions)),
            )
        elif semver_key(newest_after) < semver_key(newest_before):  # type: ignore[operator]
            found[name] = (newest_before, newest_after)
    return found


def newest(versions: set[str]) -> str | None:
    """The highest version by semver precedence, or None if any does not parse.

    All-or-nothing on purpose: a set containing one unorderable string has no
    well-defined maximum, and picking the highest of the rest would quietly
    compare against a version that may not be the real ceiling.
    """
    keys = {version: semver_key(version) for version in versions}
    if not keys or any(key is None for key in keys.values()):
        return None
    return max(keys, key=lambda version: keys[version])  # type: ignore[arg-type,return-value]


def committed_text(project_dir: Path, ref: str, filename: str) -> str | None:
    """One file as COMMITTED at ``ref``, or None when it is verifiably absent.

    Absence is established with ``git ls-tree`` rather than inferred from a failed
    ``git show``, because ``git show`` exits non-zero for both "path absent" and
    "blob unreadable" and cannot tell them apart — treating the second as the
    first would silently compare against nothing. Same reasoning, and the same
    ``<rev>:./<path>`` form, as ``renovate_uv_lock_bounded.baseline_lock_text``:
    a bare ``<rev>:<path>`` resolves from the repo root wherever it runs, so
    without the ``./`` this would read the wrong file for a nested project.
    """
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", filename],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0:
        raise RuntimeError(
            f"cannot inspect {ref}'s tree in {project_dir}: {listing.stderr.strip()}"
        )
    if filename not in listing.stdout.splitlines():
        return None
    show = subprocess.run(
        ["git", "show", f"{ref}:./{filename}"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        raise RuntimeError(
            f"{filename} exists in {ref} but cannot be read in {project_dir}: "
            f"{show.stderr.strip()}"
        )
    return show.stdout


def bounded_resolve(
    project_dir: Path, cutoff: dt.datetime
) -> subprocess.CompletedProcess[str]:
    """Re-resolve the tree with nothing published after ``cutoff`` visible.

    The lock is removed first by the caller, and that is load-bearing rather than
    tidy: with a lock in place that already satisfies ``package.json``, npm has
    nothing to do and ``--before`` changes nothing at all (measured — see the
    module docstring). Resolving from the manifest alone is the only way the date
    bound reaches the tree.
    """
    command = [*NPM_RESOLVE, f"--before={cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}"]
    return subprocess.run(command, cwd=project_dir, capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window",
        required=True,
        help="Release-age bound as an ISO 8601 duration, e.g. P3D. Match the uv "
        "lane's: a second window in the same workflow needs its own "
        "justification, and the fleet's policy is one number.",
    )
    parser.add_argument(
        "--baseline-ref",
        required=True,
        help="Git ref whose package-lock.json is the baseline for the rollback "
        "gate AND the content restored when the gate declines — the PR's base "
        "branch, e.g. origin/main. Required rather than defaulted to HEAD: HEAD "
        "on this lane is Renovate's unbounded refresh, so comparing against it "
        "would report a regression for every version the bound correctly holds "
        "back, and restoring it would ship the refresh this exists to bound.",
    )
    parser.add_argument("--project-dir", default=".")
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    lock_path = project_dir / LOCKFILE
    manifest_path = project_dir / MANIFEST

    if not lock_path.exists() or not manifest_path.exists():
        print(
            f"No {LOCKFILE}/{MANIFEST} pair in {project_dir} — nothing to bound.",
            file=sys.stderr,
        )
        return 0

    try:
        window = uv_bounded.parse_window(args.window)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1

    try:
        baseline_lock = committed_text(project_dir, args.baseline_ref, LOCKFILE)
        baseline_manifest = committed_text(project_dir, args.baseline_ref, MANIFEST)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    if baseline_lock is None:
        # A lock added in this very branch. There is nothing to compare against
        # and nothing to fall back to, so the gate cannot run — and a bounded
        # resolve with no gate behind it is the mass-rollback shape this exists to
        # avoid. Leave the file alone and say so.
        print(
            f"No {LOCKFILE} committed at {args.baseline_ref}, so there is no "
            "baseline to compare against and nothing to restore if the resolve "
            "regresses. Leaving the lock untouched.",
            file=sys.stderr,
        )
        return 0

    # The decline path writes the baseline lock back. That is only a coherent
    # thing to do while the manifest it was resolved from is still the manifest on
    # the branch — otherwise the restore produces a lock that does not satisfy
    # `package.json`, which `npm ci` rejects. lockFileMaintenance never edits a
    # manifest, so this should not fire; it is here because the failure it guards
    # is silent, and cheap to make impossible.
    if baseline_manifest is not None and baseline_manifest != manifest_path.read_text():
        print(
            f"{MANIFEST} differs from {args.baseline_ref}, so the baseline lock is "
            "no longer a valid fallback for this branch and the bound has no safe "
            "outcome to fall back to. Refusing rather than restoring a lock the "
            "manifest does not match.",
            file=sys.stderr,
        )
        return 1

    try:
        before = lock_versions(baseline_lock)
    except (ValueError, json.JSONDecodeError) as error:
        print(
            f"cannot read {LOCKFILE} at {args.baseline_ref}: {error}", file=sys.stderr
        )
        return 1

    refreshed = lock_path.read_text()
    cutoff = dt.datetime.now(dt.timezone.utc) - window

    # Removed, not overwritten: `--before` against a lock that already satisfies
    # the manifest is a silent no-op. Restored on every path out of here.
    lock_path.unlink()
    result = bounded_resolve(project_dir, cutoff)
    if result.returncode != 0 or not lock_path.exists():
        lock_path.write_text(baseline_lock)
        print(
            f"Bounded npm resolve failed, so {LOCKFILE} has been restored to what "
            f"{args.baseline_ref} ships and this run bounds nothing. Refusing to "
            "fall back to Renovate's unbounded lock.\n" + result.stderr,
            file=sys.stderr,
        )
        return 1

    try:
        after = lock_versions(lock_path.read_text())
    except (ValueError, json.JSONDecodeError) as error:
        lock_path.write_text(baseline_lock)
        print(
            f"bounded resolve produced an unreadable {LOCKFILE} ({error}); "
            f"restored {args.baseline_ref}'s copy.",
            file=sys.stderr,
        )
        return 1

    regressed = regressions(before, after)
    if regressed:
        lock_path.write_text(baseline_lock)
        detail = ", ".join(
            f"{name} {old} -> {new}" for name, (old, new) in sorted(regressed.items())
        )
        uv_bounded.summarise(
            [
                f"**npm bound declined** (`{args.window}`): the bounded resolve "
                f"would move {len(regressed)} package(s) backwards.",
                "",
                f"- {detail}",
                f"- `{LOCKFILE}` restored to `{args.baseline_ref}` verbatim, so "
                "this lane's npm diff is empty rather than a rollback.",
                "- npm has no per-package retention ceiling, so the refusal is "
                "all-or-nothing on the whole file. Nothing needs fixing: these "
                f"versions were adopted inside the window and the bound will stop "
                f"declining once they are `{args.window}` old.",
            ]
        )
        return 0

    advanced = sorted(
        name
        for name, versions in after.items()
        if name in before and before[name] != versions
    )
    added = sorted(name for name in after if name not in before)
    dropped = sorted(name for name in before if name not in after)
    unchanged = lock_path.read_text() == refreshed
    uv_bounded.summarise(
        [
            f"**npm bound applied:** `{args.window}` to `{LOCKFILE}`",
            "",
            f"- {len(advanced)} package(s) moved, {len(added)} added, "
            f"{len(dropped)} no longer required — none published inside the window",
            "- no package moved backwards from what the base branch ships, checked "
            "on the newest version each name is pinned at",
            "- identical to Renovate's own refresh: nothing it took was inside the "
            "window"
            if unchanged
            else "- differs from Renovate's refresh, which resolved unbounded",
        ]
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
