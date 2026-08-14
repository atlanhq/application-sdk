#!/usr/bin/env python3
"""Renovate lock-refresh driver: re-lock under a release-age bound, then erase
the bound from the lockfile.

Invoked from the shared preset's ``lockFileMaintenance.postUpgradeTasks`` on the
self-hosted fleet runner. Renovate has already produced an *unbounded* ``uv.lock``
(its native uv manager resolves to latest, and Renovate passes it no release-age
bound — renovatebot/renovate#41652). This re-resolves that lock with
``--exclude-newer`` and hands the result back for Renovate to commit.

Why the bound lives here and not in each repo's ``pyproject.toml``
-----------------------------------------------------------------
Declaring ``[tool.uv] exclude-newer`` works, but it bounds *every* resolve in
that repo — including a developer's ``uv lock``, and including the
``/fix-vulnerabilities`` workflow's ``uv lock --upgrade-package``, which is how a
CVE fix actually lands. Putting the bound in this one command bounds exactly one
path: the unattended "upgrade everything to latest" refresh that auto-merges
without a human. Every deliberate act stays unbounded, which is the whole point —
the cooldown exists to buy detection time against releases nobody knows are bad
yet, not to slow down someone who has decided to take a specific version.

Why the ``[options]`` block is stripped (FND-367)
------------------------------------------------
uv records its resolver settings into ``uv.lock``::

    [options]
    exclude-newer = "0001-01-01T00:00:00Z"
    exclude-newer-span = "P7D"

Every consumer that validates the lock compares its *own* settings against that
block. The app Dockerfiles run ``uv sync --locked``, which then rejects the lock
as stale — that is what reddened ``scan / Build Image`` fleet-wide when this was
first attempted (application-sdk#3212, reverted in #3218). Stripping the block
leaves a lock whose *content* is bounded but whose recorded settings are the
defaults, so every consumer agrees. Verified on uv 0.12.4: ``uv lock --check``
and ``uv sync --locked`` both fail before the strip and pass after it.

Note the asymmetry this creates, because it is deliberate: a bare ``uv lock
--upgrade`` run locally will produce a *newer* lock than this command does. That
is correct — a human upgrading on purpose is not subject to the cooldown — but it
means the lockfile is not reproducible from ``pyproject.toml`` alone. The
independent check on that is ``checks/dep-cooldown``, which reads the committed
lock and fails if anything in it is younger than the window.

Failure modes this guards against
---------------------------------
1. **Unsatisfiable floor.** ``constraint-dependencies`` is the fleet's CVE-floor
   list, and each entry points at a just-published fix. A bounded resolve has no
   valid candidate for a floor younger than the window, so ``uv lock`` fails
   outright — FND-310's trap. On failure this retries once, exempting the floored
   packages uv named, and reports them. A deliberate floor beats the cooldown.
2. **Silent rollback.** A bounded resolve does not error when it cannot reach a
   package; it quietly resolves an older one. Observed while validating this:
   with only the two SDK packages exempted, ``atlan-application-sdk`` resolved to
   3.27.2 instead of 3.28.0 — because 3.28.0 requires ``pyatlan>=10`` and pyatlan
   10.0.0 was two days old, so the bound made it invisible and uv backtracked. No
   error, no warning; unattended, that auto-merges an SDK *downgrade* across the
   fleet. Hence ``pyatlan`` in the exempt set, and hence the check that no exempt
   package came out lower than it went in.

Never resolves unbounded as a fallback. A control whose failure mode is a log
line is not a control (FND-367) — if the bound cannot be applied, this exits
non-zero, Renovate reports an artifact error, and the approval gate withholds
the atlan-ci approval so nothing auto-merges.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# uv names the packages it could not resolve in its error text. Rather than
# parse that free-form output, we intersect it with the packages the repo has
# explicitly floored — so the retry can only ever admit a version the repo has
# already made a deliberate decision about, never an arbitrary one uv mentioned
# in passing.
_ERROR_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]+")

# PEP 503 normalisation, so `Foo_Bar` and `foo-bar` compare equal.
_NORMALISE_RE = re.compile(r"[-_.]+")

_LEADING_DIGITS_RE = re.compile(r"^(\d+(?:\.\d+)*)")


def normalise(name: str) -> str:
    """PEP 503 name normalisation."""
    return _NORMALISE_RE.sub("-", name.strip().lower())


def lock_versions(lock_text: str) -> dict[str, str]:
    """``{normalised name: version}`` for every registry-sourced package.

    Packages sourced from a path/git/editable checkout carry no registry version
    to compare, so they are skipped rather than reported as changed.
    """
    try:
        data = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError:
        return {}
    versions: dict[str, str] = {}
    for package in data.get("package", []):
        if not isinstance(package, dict):
            continue
        source = package.get("source") or {}
        if any(source.get(k) for k in ("editable", "git", "path", "url", "directory")):
            continue
        name, version = package.get("name"), package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            versions[normalise(name)] = version
    return versions


def strip_options(lock_text: str) -> str:
    """Remove the ``[options]`` table and any ``[options.*]`` subtable.

    Line-based rather than a TOML round-trip on purpose: ``tomllib`` is
    read-only, and re-emitting the document with a writer would reorder and
    reformat 5000 lines, turning every lock refresh into an unreviewable diff.
    """
    kept: list[str] = []
    skipping = False
    for line in lock_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("[options]") or stripped.startswith("[options."):
            skipping = True
            continue
        if skipping and (
            stripped.startswith("[[")
            or (stripped.startswith("[") and not stripped.startswith("[options"))
        ):
            skipping = False
        if not skipping:
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(kept))


def floored_packages(pyproject_text: str) -> set[str]:
    """Packages the repo has deliberately pinned a minimum version for.

    Both sources count as deliberate: ``[tool.uv] constraint-dependencies`` (where
    the CVE floors live) and ``[project] dependencies`` / ``optional-dependencies``
    (where ``/fix-vulnerabilities`` writes a floor when it promotes a vulnerable
    transitive to a direct dependency).
    """
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return set()

    requirements: list[str] = []
    project = data.get("project", {})
    if isinstance(project, dict):
        requirements.extend(
            r for r in project.get("dependencies", []) if isinstance(r, str)
        )
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    requirements.extend(r for r in group if isinstance(r, str))
    uv_table = (
        data.get("tool", {}).get("uv", {}) if isinstance(data.get("tool"), dict) else {}
    )
    if isinstance(uv_table, dict):
        requirements.extend(
            r for r in uv_table.get("constraint-dependencies", []) if isinstance(r, str)
        )

    names: set[str] = set()
    for requirement in requirements:
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if match:
            names.add(normalise(match.group(1)))
    return names


def blocked_by_floor(uv_stderr: str, floors: set[str]) -> list[str]:
    """Floored packages named in uv's resolution error.

    Intersecting with the floor set keeps the retry honest: it can only admit a
    package the repo already floored on purpose. If uv failed for some other
    reason, this is empty and the caller fails rather than retrying blind.
    """
    mentioned = {normalise(t) for t in _ERROR_TOKEN_RE.findall(uv_stderr)}
    return sorted(mentioned & floors)


def _version_key(version: str) -> tuple[int, ...] | None:
    match = _LEADING_DIGITS_RE.match(version)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def rollbacks(
    before: dict[str, str], after: dict[str, str], packages: list[str]
) -> dict[str, tuple[str, str]]:
    """Exempt packages that came out of the bounded resolve older than they went in.

    Compares leading numeric components only; a version either side that does not
    start with digits is reported as a rollback rather than ignored, because an
    unrecognised version on an exempt package is exactly the case worth a human
    look. Third-party packages moving backwards is the *intended* effect of the
    bound and is never reported here.
    """
    found: dict[str, tuple[str, str]] = {}
    for name in (normalise(p) for p in packages):
        old, new = before.get(name), after.get(name)
        if old is None or new is None or old == new:
            continue
        old_key, new_key = _version_key(old), _version_key(new)
        if old_key is None or new_key is None or new_key < old_key:
            found[name] = (old, new)
    return found


def build_uv_command(window: str, exempt: list[str]) -> list[str]:
    command = ["uv", "lock", "--upgrade", "--exclude-newer", window]
    for name in exempt:
        command += ["--exclude-newer-package", f"{name}=P0D"]
    return command


def run_uv_lock(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def summarise(lines: list[str]) -> None:
    """Write to the step summary when running in Actions; always echo to stdout."""
    body = "\n".join(lines)
    print(body)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(body + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window",
        required=True,
        help="Release-age bound as an ISO 8601 duration, e.g. P7D.",
    )
    parser.add_argument(
        "--exempt",
        action="append",
        default=[],
        help="Package admitted regardless of age. Repeatable. First-party packages "
        "plus anything in their dependency closure that must move with them.",
    )
    parser.add_argument("--project-dir", default=".")
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    lock_path = project_dir / "uv.lock"
    pyproject_path = project_dir / "pyproject.toml"

    if not lock_path.exists():
        print(f"No uv.lock in {project_dir} — nothing to bound.", file=sys.stderr)
        return 0

    before = lock_versions(lock_path.read_text())
    exempt = list(args.exempt)

    result = run_uv_lock(build_uv_command(args.window, exempt), project_dir)
    admitted_early: list[str] = []

    if result.returncode != 0:
        floors = (
            floored_packages(pyproject_path.read_text())
            if pyproject_path.exists()
            else set()
        )
        admitted_early = blocked_by_floor(result.stderr, floors)
        if not admitted_early:
            print(
                "Bounded `uv lock` failed and no deliberately-floored package was "
                "named in the error, so there is nothing safe to admit early. "
                "Refusing to fall back to an unbounded resolve.\n" + result.stderr,
                file=sys.stderr,
            )
            return 1
        result = run_uv_lock(
            build_uv_command(args.window, exempt + admitted_early), project_dir
        )
        if result.returncode != 0:
            print(
                "Bounded `uv lock` still failed after admitting "
                f"{', '.join(admitted_early)}.\n" + result.stderr,
                file=sys.stderr,
            )
            return 1

    after = lock_versions(lock_path.read_text())

    regressed = rollbacks(before, after, exempt + admitted_early)
    if regressed:
        detail = ", ".join(
            f"{n} {old} -> {new}" for n, (old, new) in sorted(regressed.items())
        )
        print(
            "Bounded resolve rolled an exempt package BACKWARDS: "
            f"{detail}. This is the silent-downgrade failure mode: a package that "
            "should never be delayed could not be reached, so uv resolved an older "
            "one instead of failing. Usually it means something in that package's "
            "dependency closure is inside the cooldown and also needs exempting. "
            "Refusing to commit a first-party downgrade.",
            file=sys.stderr,
        )
        return 1

    lock_path.write_text(strip_options(lock_path.read_text()))

    held = sorted(
        name for name, version in after.items() if before.get(name) != version
    )
    lines = [
        f"**Release-age bound applied:** `{args.window}` "
        f"(exempt: {', '.join(exempt) or 'none'})",
        "",
        f"- {len(held)} package(s) resolved below the unbounded latest",
        "- `[options]` stripped from `uv.lock` so `uv sync --locked` still validates",
    ]
    if admitted_early:
        lines.append(
            f"- **Admitted inside the window** because the repo floors them: "
            f"{', '.join(admitted_early)}"
        )
    summarise(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
