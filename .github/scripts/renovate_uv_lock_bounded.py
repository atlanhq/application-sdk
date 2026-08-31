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

The bound governs ADOPTION, never REVERSION
-------------------------------------------
A plain ``uv lock --upgrade --exclude-newer P7D`` does not merely decline to take
new releases — it re-resolves everything and *rolls back* anything already locked
that was published inside the window. Measured on the pilot: 13 downgrades
against ``atlan-hello-world-app``'s ``main`` and zero upgrades, including
``boto3 1.43.72 -> 1.43.67`` and ``starlette 1.6.0 -> 1.4.1``.

That is worse than the delay the cooldown is meant to impose. Most transitive
security uplift in this fleet arrives silently through lock maintenance with no
floor ever recorded (a floor in ``constraint-dependencies`` or ``[project]
dependencies`` would hold the line, but only the fixes someone thought to record
have one). Reverting blind can therefore re-introduce a fix that was already in
place — a strictly worse failure than shipping a fix seven days late.

So every package already in the lock gets a per-package ceiling derived from the
``upload-time`` of the version it is currently pinned to, which the lockfile
already records for every sdist and wheel. The effective ceiling is
``max(now - window, upload-time of the locked version)``:

* nothing already adopted moves backwards,
* nothing published inside the window is newly adopted — the control is intact,
* the first bounded run in a repo is a near no-op rather than a mass rollback,
* and a yanked release is still dropped, because uv skips yanked versions when it
  re-resolves.

What this deliberately does not do is revert a *malicious* version adopted before
the bound existed. That is not the cooldown's job — it buys time for the ecosystem
to notice, and when it does, the release is yanked (handled above) or gets an
advisory (handled by the security lane, which bypasses the cooldown outright).

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
   fleet. Hence ``pyatlan`` in the exempt set.

   With per-package ceilings in place a downgrade should now be impossible for
   age reasons, so any that survives means something else moved — a yanked
   release, or a constraint change forcing a different solution. Rare, worth a
   human, and never worth auto-merging: the run fails and nothing merges.

Never resolves unbounded as a fallback. A control whose failure mode is a log
line is not a control (FND-367) — if the bound cannot be applied, this exits
non-zero, Renovate reports an artifact error, and the approval gate withholds
the atlan-ci approval so nothing auto-merges.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

try:  # PEP 508/440 parsing for floor classification and the rollback gate.
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version
except ImportError:  # pragma: no cover - packaging is effectively universal
    Requirement = None  # type: ignore[assignment]
    canonicalize_name = None  # type: ignore[assignment]
    Version = None  # type: ignore[assignment]
    InvalidVersion = ValueError  # type: ignore[assignment]

# Why a refusal says *why* (FND-909)
# ----------------------------------
# Every `withhold()` path writes a byte-identical tripwire, so nothing downstream
# can tell them apart — and only one of them heals on its own. `WINDOW_EMPTY`
# means the bound admitted nothing on this pass while Renovate's unbounded
# resolve moved; the next pass that sees a package cross the window resolves
# green with no intervention. The other four are standing faults (a broken
# interpreter, an unsatisfiable floor, a yanked pin) that no amount of waiting
# fixes.
#
# The distinction is what lets the fleet reap the self-healing case on sight
# while leaving a real wedge red for a human. Reaping on a clock instead —
# "refuse, then recover once head age exceeds the window" — is safe but costs up
# to a full window of delay on top of the bound, which defeats the point of
# running the lane every four hours.
#
# The stamp rides as a comment on the tripwire's own line. That placement is
# load-bearing: `strip_options()` drops the whole `[options]` table line-based,
# so the stamp cannot survive onto a green lock, and every existing reader of
# the window already splits on `#` before parsing the value.
REFUSAL_WINDOW_EMPTY = "window-empty"
REFUSAL_NO_PACKAGING = "no-packaging"
REFUSAL_UNSATISFIABLE_FLOOR = "unsatisfiable-floor"
REFUSAL_FLOOR_ADMITTED_STILL_FAILED = "floor-admitted-still-failed"
REFUSAL_ROLLBACK = "rollback"

# The one reason a machine may clear without a human. Kept as a set of one
# rather than an equality check so adding a second self-healing path is a
# one-line change here instead of an edit at every reader.
SELF_HEALING_REFUSALS = frozenset({REFUSAL_WINDOW_EMPTY})

# ISO 8601 durations, restricted to the forms a release-age window sensibly takes.
# Calendar units are rejected rather than approximated — uv refuses months and
# years for the same reason, and a window that silently means something other
# than what it says is worse than one that fails to parse.
_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")

# uv names the packages it could not resolve in its error text. Rather than
# parse that free-form output, we intersect it with the packages the repo has
# explicitly floored — so the retry can only ever admit a version the repo has
# already made a deliberate decision about, never an arbitrary one uv mentioned
# in passing.
_ERROR_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]+")

# PEP 503 normalisation, so `Foo_Bar` and `foo-bar` compare equal.
_NORMALISE_RE = re.compile(r"[-_.]+")


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


def parse_window(window: str) -> dt.timedelta:
    """``P7D`` / ``PT12H`` -> timedelta. Raises on anything else."""
    match = _DURATION_RE.match(window.strip())
    if not match or not any(match.groups()):
        raise ValueError(
            f"unsupported release-age window {window!r}; expected an ISO 8601 "
            "duration in days/hours/minutes, e.g. P7D or PT12H"
        )
    days, hours, minutes = (int(g) if g else 0 for g in match.groups())
    return dt.timedelta(days=days, hours=hours, minutes=minutes)


def lock_upload_times(lock_text: str) -> dict[str, dt.datetime]:
    """``{normalised name: newest upload-time}`` for the version each package is
    pinned to.

    uv records ``upload-time`` on every sdist and wheel entry, so the age of what
    is *currently locked* is available offline — no registry round-trip, and no
    fail-open path when one is unavailable. The newest timestamp across a
    package's files is used: the ceiling has to admit every file the resolver
    needs, and wheels for different platforms are published seconds apart.
    """
    try:
        data = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError:
        return {}

    times: dict[str, dt.datetime] = {}
    for package in data.get("package", []):
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        if not isinstance(name, str):
            continue
        stamps: list[dt.datetime] = []
        entries = [package.get("sdist")] + list(package.get("wheels") or [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("upload-time")
            if not isinstance(raw, str):
                continue
            try:
                parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            stamps.append(
                parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
            )
        if stamps:
            times[normalise(name)] = max(stamps)
    return times


def retention_ceilings(
    upload_times: dict[str, dt.datetime], cutoff: dt.datetime
) -> dict[str, str]:
    """Per-package ceilings that stop the bound rolling anything backwards.

    A package whose locked version is *older* than the cutoff needs no ceiling —
    the window already admits it. One whose locked version is younger would
    otherwise be reverted, so it gets a ceiling one second past its own upload
    instant: uv can still keep exactly that version, and can take nothing newer.
    """
    ceilings: dict[str, str] = {}
    for name, uploaded in upload_times.items():
        if uploaded > cutoff:
            admit = (uploaded + dt.timedelta(seconds=1)).astimezone(dt.timezone.utc)
            ceilings[name] = admit.strftime("%Y-%m-%dT%H:%M:%SZ")
    return ceilings


def withhold(lock_path: Path, baseline: str, window: str, *, reason: str) -> bool:
    """Refuse in a way a REQUIRED check can see. Returns whether it wrote.

    The driver cannot stop Renovate committing, and it cannot abstain either.
    Renovate captures its own unbounded ``uv lock --upgrade`` as an artifact
    *before* this command runs, commits the working tree afterwards whatever the
    exit code, and falls back to that captured artifact whenever the tree matches
    HEAD. So the two obvious refusals both ship something unwanted: leaving the
    rejected resolve in place ships the rejected resolve, and restoring the
    baseline exactly ships Renovate's *unbounded* copy instead — silently, since
    a valid-but-unbounded lock passes every check.

    Nor does exiting non-zero withhold anything on its own. It reds
    ``renovate/artifacts``, which is an advisory status: it is not a required
    check in any lock-lane repo, those repos require zero approving reviews, and
    Renovate re-arms GitHub-native automerge in the same pass that records the
    failure. Observed 2026-08-19: six of the day's eight auto-merged fleet lock
    PRs carried a third-party release between 21 minutes and 3 hours old.

    The one lever the driver does hold over a *required* check is the lock's own
    validity. So a refusal writes the baseline's versions — never the rejected
    resolve, never an unbounded one — and adds an ``[options]`` table the repo's
    ``pyproject.toml`` does not declare. That is exactly what ``uv sync --locked``
    rejects in the image build, and ``scan / Build Image`` is required in every
    lock-lane repo, so the branch cannot merge until a human looks at it.

    Deliberate, documented, and meant to be temporary: the durable fix is a
    required age check that fails on its own evidence rather than on a lock the
    build cannot install (FND-379).

    ``reason`` is keyword-only and has no default on purpose. Every caller has to
    say which of the five refusal paths it is, because the fleet reaps
    ``REFUSAL_WINDOW_EMPTY`` automatically and must not reap the rest — a default
    would let a new standing fault inherit the self-healing label silently
    (FND-909).

    A branch that *adds* a brand-new ``uv.lock`` has no committed baseline to
    write back. The tripwire still goes on, over whatever the resolve left: there
    is no safer content to choose, and leaving a valid-but-unbounded lock in place
    is the one refusal that lands on no required check at all. ``strip_options``
    recovers whatever was there in both cases.
    """
    base = baseline or lock_path.read_text()
    if not base:
        return False
    base = strip_options(base)
    tripwire = f'\n[options]\nexclude-newer-span = "{window}"  # refusal: {reason}\n'
    anchor = base.find("\n[[package]]")
    lock_path.write_text(
        base[:anchor] + tripwire + base[anchor:] if anchor != -1 else base + tripwire
    )
    return True


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


# A requirement counts as a deliberate floor only when it pins a version with an
# explicit lower bound (``>=``) or an exact pin (``==``) in its PEP 508 version
# specifier. A bare name (``"orjson"``) constrains nothing — it admits any version,
# so there is no floor for the cooldown to conflict with. ``/fix-vulnerabilities``
# always writes ``>=min`` and ``constraint-dependencies`` CVE floors are always
# lower bounds, so this loses no intended case while closing the fail-open seam
# where a floor-less dep named in uv's error would otherwise be admitted inside
# the window for an unrelated failure.
#
# The requirement is parsed as PEP 508 rather than scanned as text. Substring
# matching over the raw string reads bounds out of two places that are not package
# floors: an environment marker (``foo; python_version >= "3.10"`` bounds the
# interpreter, not the package) and a direct-reference URL (``foo @
# https://…/pkg.tar.gz?constraint==1`` — the ``==`` is URL data). Parsing inspects
# only the parsed ``SpecifierSet``, so neither can be misread as a floor.


def _floor_specified(requirement: str) -> str | None:
    """The package's normalised name if ``requirement`` floors a version, else None.

    Structural, not textual: the PEP 508 parse separates the version specifier
    from both the environment marker and any direct-reference URL, so a ``>=`` /
    ``==`` is honoured only where it actually constrains the package. Fails closed:
    a requirement that does not parse, or a missing ``packaging`` library, yields
    no floor classification rather than a guess — the safe direction for a control
    whose whole point is to *not* admit a package early.
    """
    if Requirement is None:
        return None
    try:
        parsed = Requirement(requirement)
    except Exception:  # InvalidRequirement and friends — not a floor we can trust
        return None
    for specifier in parsed.specifier:
        if specifier.operator in (">=", "=="):
            return normalise(parsed.name)
    return None


def declares_own_bound(pyproject_text: str) -> bool:
    """Does the repo already bound its own resolves via ``[tool.uv]``?

    Four repos do (glue, bw, thoughtspot, dbt), predating the fleet mechanism.
    For them this driver must do nothing at all: they already have a cooldown, so
    ours is redundant, and stripping ``[options]`` would leave a lock their own
    ``pyproject.toml`` disagrees with — `uv sync --locked` in the image build then
    fails, which is FND-367's breakage arriving from the opposite direction.

    Skipping rather than failing keeps those repos working while they converge on
    the central mechanism on their own schedule, instead of turning a fleet-wide
    rollout into four simultaneous conversations with repo owners. The skip is
    reported, not silent, so the convergence work stays visible.
    """
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return False
    tool = data.get("tool")
    uv_table = tool.get("uv", {}) if isinstance(tool, dict) else {}
    if not isinstance(uv_table, dict):
        return False
    # Either key alone is enough: uv records both into the lockfile's [options],
    # so either one makes the strip destructive.
    return "exclude-newer" in uv_table or "exclude-newer-package" in uv_table


def floored_packages(pyproject_text: str) -> set[str]:
    """Packages the repo has deliberately pinned a minimum version for.

    Both sources count as deliberate: ``[tool.uv] constraint-dependencies`` (where
    the CVE floors live) and ``[project] dependencies`` / ``optional-dependencies``
    (where ``/fix-vulnerabilities`` writes a floor when it promotes a vulnerable
    transitive to a direct dependency). A requirement only counts when its PEP 508
    version specifier carries an explicit ``>=`` or ``==`` — a bare package name,
    an environment-marker bound, or a direct-reference URL is not a floor.
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
        floored = _floor_specified(requirement)
        if floored is not None:
            names.add(floored)
    return names


def blocked_by_floor(uv_stderr: str, floors: set[str]) -> list[str]:
    """Floored packages named in uv's resolution error.

    Intersecting with the floor set keeps the retry honest: it can only admit a
    package the repo already floored on purpose. If uv failed for some other
    reason, this is empty and the caller fails rather than retrying blind.
    """
    mentioned = {normalise(t) for t in _ERROR_TOKEN_RE.findall(uv_stderr)}
    return sorted(mentioned & floors)


def _version_key(version: str) -> "Version | None":
    """Parse a PEP 440 version, or None when it does not parse.

    Structural, not textual: a leading-digits prefix reads ``0.62b1`` as ``0.62``
    and ``1!2.0`` as ``2``, so a prerelease or epoch rollback would compare equal
    to (or greater than) the version it actually regresses. ``Version`` orders
    those correctly — a prerelease sorts before its release, and an epoch beats
    every version without one. Returns None on an unparseable string so the caller
    reports it for a human rather than silently dropping the comparison.
    """
    if Version is None:
        return None
    try:
        return Version(version)
    except InvalidVersion:
        return None


def rollbacks(
    before: dict[str, str], after: dict[str, str], packages: list[str] | None = None
) -> dict[str, tuple[str, str]]:
    """Packages that came out of the bounded resolve older than they went in.

    Checks **every** package by default, not just the exempt ones. An earlier
    revision of this scoped the check to first-party packages and treated
    third-party downgrades as the bound working as intended — which is exactly
    the bug the retention ceilings above exist to fix. With those in place a
    downgrade can no longer happen for age reasons, so anything reported here is
    a real signal.

    Versions are compared as parsed PEP 440 ``Version`` objects, so a prerelease
    (``0.62b2 -> 0.62b1``) or epoch (``1!2.0 -> 2.0``) rollback is caught rather
    than read as a numeric prefix. A version either side that does not parse is
    reported rather than ignored, because an unparseable version is the case most
    worth a human look.
    """
    found: dict[str, tuple[str, str]] = {}
    names = (normalise(p) for p in packages) if packages is not None else before.keys()
    for name in names:
        old, new = before.get(name), after.get(name)
        if old is None or new is None or old == new:
            continue
        old_key, new_key = _version_key(old), _version_key(new)
        if old_key is None or new_key is None or new_key < old_key:
            found[name] = (old, new)
    return found


def build_uv_command(
    window: str, exempt: list[str], ceilings: dict[str, str] | None = None
) -> list[str]:
    """The bounded resolve, plus one ceiling flag per package that needs one.

    Exemptions come last so they win over a retention ceiling for the same
    package: a first-party package must be free to move forward, not merely be
    held where it is.
    """
    command = ["uv", "lock", "--upgrade", "--exclude-newer", window]
    exempt_normalised = {normalise(name) for name in exempt}
    for name, admit in sorted((ceilings or {}).items()):
        if name not in exempt_normalised:
            command += ["--exclude-newer-package", f"{name}={admit}"]
    for name in exempt:
        command += ["--exclude-newer-package", f"{name}=P0D"]
    return command


def run_uv_lock(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def baseline_lock_text(cwd: Path, ref: str = "HEAD") -> str | None:
    """``uv.lock`` at ``ref``, or None if that commit has no copy of it.

    Emphatically NOT the working tree. By the time this runs, Renovate has
    already refreshed the lock to latest-of-everything in the working tree — that
    is the whole input this command exists to bound. Deriving retention ceilings
    from *that* would pin every package to the release Renovate pulled seconds
    ago and neutralise the window completely, and comparing against it would
    report a rollback for every package the bound correctly holds back.

    ``HEAD`` is the right baseline in both of Renovate's own branch states, which
    is why it is the default: under ``postUpgradeTasks`` the refresh is still
    uncommitted, so a fresh branch has the base-branch commit at HEAD and a
    reused one has its own previous (already bounded) commit — the version that
    actually shipped last.

    ``ref`` exists for the one caller where that does not hold: a GitHub Actions
    workflow triggered by the push Renovate just made (FND-376). There the
    refresh is *already committed*, so HEAD is the unbounded lock itself and
    using it would derive every ceiling from the minutes-old versions — the
    neutralised-window failure described above, arriving from the other
    direction. That caller passes the base branch instead, which also makes
    "never roll back relative to what main ships" structural rather than
    incidental.

    Returns None only when the path is verifiably absent from ``ref`` — a lock
    added in this very branch, which legitimately has no baseline. Anything else
    that goes wrong raises, because silently treating "cannot tell" as "no
    baseline" would drop the ceilings and quietly reintroduce the rollback this
    exists to prevent. That includes a ref that resolves but whose lock blob
    cannot be read: absent is a fact about the tree, not a guess from a failed
    command, so absence is established positively with `git ls-tree` and every
    `git show` failure on a path known to be present is treated as fatal.
    """
    head = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        raise RuntimeError(
            f"cannot resolve {ref} in {cwd}, so the pre-refresh lockfile is "
            f"unavailable: {head.stderr.strip()}"
        )
    # Absence must be established positively, not inferred from a failed read:
    # `git show` exits non-zero for BOTH "path absent" and "blob unreadable",
    # so it cannot tell the two apart. `git ls-tree` can — it exits 0 and
    # prints nothing when the path is absent, prints the path when present, and
    # only exits non-zero when git itself is broken.
    #
    # Both git invocations are cwd-relative, and that is load-bearing for a
    # project that is not the repo root. `ls-tree`'s pathspec already resolves
    # against cwd, but `<rev>:<path>` does NOT — a bare `<rev>:uv.lock` is
    # repo-root-relative wherever it runs, so from `packages/conformance` it
    # silently returns the ROOT lock as that project's baseline. The `./` prefix
    # is what makes `git show` agree with `ls-tree` about which file is meant.
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", "uv.lock"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if listing.returncode != 0:
        raise RuntimeError(
            f"cannot inspect {ref}'s tree in {cwd}, so the pre-refresh lockfile "
            f"is unavailable: {listing.stderr.strip()}"
        )
    if "uv.lock" not in listing.stdout.splitlines():
        # Verifiably absent from ref — a lock added in this very branch, which
        # legitimately has no baseline.
        return None
    show = subprocess.run(
        ["git", "show", f"{ref}:./uv.lock"], cwd=cwd, capture_output=True, text=True
    )
    if show.returncode != 0:
        raise RuntimeError(
            f"uv.lock exists in {ref} but cannot be read in {cwd}, so the "
            f"pre-refresh lockfile is unavailable: {show.stderr.strip()}"
        )
    return show.stdout


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
    parser.add_argument(
        "--caller-owns-commit",
        action="store_true",
        help="The caller controls what gets committed, so a bound that admits "
        "nothing is an ordinary no-op: write back the bounded resolve, whose "
        "versions match the baseline, and exit 0, and let the caller commit "
        "that. Set by bound_lock_branch.py (FND-376), whose workflow owns the "
        "push. Leave it off under Renovate's postUpgradeTasks, where Renovate "
        "commits the working tree itself and substitutes its own unbounded "
        "artifact whenever this command leaves the tree matching HEAD — there, "
        "admitting nothing has to fail visibly instead.",
    )
    parser.add_argument(
        "--baseline-ref",
        default="HEAD",
        help="Git ref whose uv.lock is the PRE-refresh baseline for retention "
        "ceilings and the rollback gate. Default HEAD, which is correct under "
        "Renovate's postUpgradeTasks (the refresh is still uncommitted there). A "
        "caller that runs AFTER the refresh has been committed — a workflow "
        "triggered by Renovate's own push — must pass the base branch instead, "
        "or every ceiling is derived from the versions it is meant to bound.",
    )
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    lock_path = project_dir / "uv.lock"
    pyproject_path = project_dir / "pyproject.toml"

    if not lock_path.exists():
        print(f"No uv.lock in {project_dir} — nothing to bound.", file=sys.stderr)
        return 0

    if pyproject_path.exists() and declares_own_bound(pyproject_path.read_text()):
        summarise(
            [
                "**Release-age bound skipped:** this repo declares its own "
                "`[tool.uv] exclude-newer`, so it already has a cooldown and the "
                "lockfile records settings that must keep matching it.",
                "",
                "- The fleet now bounds this lane centrally (FND-359), so the "
                "repo-local stanza is redundant and can be removed.",
                "- Until it is, this driver leaves `uv.lock` untouched — stripping "
                "`[options]` here would leave a lock `pyproject.toml` disagrees "
                "with, and `uv sync --locked` would fail in the image build.",
            ]
        )
        return 0

    try:
        window = parse_window(args.window)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1

    exempt = list(args.exempt)

    # Baseline = the lock as COMMITTED at --baseline-ref, never the working tree.
    # See baseline_lock_text for why that distinction is load-bearing, and why
    # the right ref depends on whether the refresh has been committed yet.
    try:
        baseline = baseline_lock_text(project_dir, args.baseline_ref)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1

    if baseline is None:
        print(
            f"No committed uv.lock at {args.baseline_ref} — treating this as a new "
            "lockfile: no retention ceilings and no rollback comparison.",
            file=sys.stderr,
        )
        baseline = ""

    if Version is None:
        # `packaging` is imported defensively at the top of this module, and every
        # version comparison here degrades to "cannot compare" without it. The
        # rollback gate reports what it cannot compare, so a run in that state
        # accuses every ordinary upgrade of moving backwards — a wedged lane whose
        # message blames the dependency data. Say the true cause instead, and say
        # it before uv spends a minute resolving.
        withhold(lock_path, baseline, args.window, reason=REFUSAL_NO_PACKAGING)
        print(
            "`packaging` is not importable, so no version can be compared and the "
            "rollback gate cannot do its job. Refusing rather than bounding "
            "blind: install packaging for the interpreter running this command "
            "(the workflow pins it) and re-run.",
            file=sys.stderr,
        )
        return 1

    before = lock_versions(baseline)
    cutoff = dt.datetime.now(dt.timezone.utc) - window
    ceilings = retention_ceilings(lock_upload_times(baseline), cutoff)

    # What Renovate resolved unbounded, and has already captured as the artifact
    # it will commit if this command leaves the tree looking untouched. Read it
    # now: the bounded resolve below overwrites it.
    renovate_versions = lock_versions(lock_path.read_text())

    result = run_uv_lock(build_uv_command(args.window, exempt, ceilings), project_dir)
    admitted_early: list[str] = []

    if result.returncode != 0:
        floors = (
            floored_packages(pyproject_path.read_text())
            if pyproject_path.exists()
            else set()
        )
        admitted_early = blocked_by_floor(result.stderr, floors)
        if not admitted_early:
            withhold(
                lock_path,
                baseline,
                args.window,
                reason=REFUSAL_UNSATISFIABLE_FLOOR,
            )
            print(
                "Bounded `uv lock` failed and no deliberately-floored package was "
                "named in the error, so there is nothing safe to admit early. "
                "Refusing to fall back to an unbounded resolve. The lock is left "
                "deliberately un-installable — baseline versions plus a tripwire "
                "`[options]` table — so a required check holds the branch.\n"
                + result.stderr,
                file=sys.stderr,
            )
            return 1
        result = run_uv_lock(
            build_uv_command(args.window, exempt + admitted_early, ceilings),
            project_dir,
        )
        if result.returncode != 0:
            withhold(
                lock_path,
                baseline,
                args.window,
                reason=REFUSAL_FLOOR_ADMITTED_STILL_FAILED,
            )
            print(
                "Bounded `uv lock` still failed after admitting "
                f"{', '.join(admitted_early)}. The lock is left deliberately "
                "un-installable — baseline versions plus a tripwire `[options]` "
                "table — so a required check holds the branch.\n" + result.stderr,
                file=sys.stderr,
            )
            return 1

    after = lock_versions(lock_path.read_text())

    regressed = rollbacks(before, after)
    if regressed:
        detail = ", ".join(
            f"{n} {old} -> {new}" for n, (old, new) in sorted(regressed.items())
        )
        withhold(lock_path, baseline, args.window, reason=REFUSAL_ROLLBACK)
        print(
            f"Bounded resolve moved {len(regressed)} package(s) BACKWARDS from the "
            f"last committed lock: {detail}. Retention ceilings make an age-driven "
            "downgrade impossible, so something else forced a different solution. "
            "Check first whether the base branch pins a release that has since "
            "been YANKED upstream — `pip index versions <name>` or the PyPI JSON "
            "API will say — because no resolve can keep a yanked pin and every "
            "one of them will land here until the base branch moves off it. A "
            "changed constraint is the other candidate, as is a version string "
            "that is not valid PEP 440 — those are reported here too rather than "
            "silently skipped. Either way it wants a "
            "human, so this run bounds nothing and leaves the lock deliberately "
            "un-installable — baseline versions plus a tripwire `[options]` "
            "table — for a required check to hold the branch on.",
            file=sys.stderr,
        )
        return 1

    if after == before and renovate_versions != before:
        moved = ", ".join(
            f"{name} {before.get(name, 'absent')} -> {version}"
            for name, version in sorted(renovate_versions.items())
            if before.get(name) != version
        )
        if args.caller_owns_commit:
            # Informational only: this lane commits whatever we write, so a hold
            # is a no-op rather than a refusal. Keep the window's contents in
            # the log so a net-empty PR still explains itself.
            print(
                f"The bound admits nothing today; Renovate's unbounded resolve "
                f"moved: {moved}. The caller owns the commit, so this is an "
                "ordinary no-op rather than a hold.",
                file=sys.stderr,
            )
        else:
            withhold(lock_path, baseline, args.window, reason=REFUSAL_WINDOW_EMPTY)
            print(
                f"The bound admits nothing today, but Renovate's own unbounded "
                f"resolve moved: {moved}. Leaving the tree matching HEAD would "
                "hand Renovate's copy straight to the branch — a valid lock "
                "that passes every check while carrying releases minutes old — "
                "so the lock is left deliberately un-installable instead and "
                "this run fails. Nothing needs fixing in the repo: the window "
                f"will admit these versions once they are `{args.window}` old.",
                file=sys.stderr,
            )
            return 1

    lock_path.write_text(strip_options(lock_path.read_text()))

    advanced = sorted(
        name for name, v in after.items() if before.get(name) not in (None, v)
    )
    added = sorted(name for name in after if name not in before)
    lines = [
        f"**Release-age bound applied:** `{args.window}` "
        f"(exempt: {', '.join(exempt) or 'none'})",
        "",
        f"- {len(ceilings)} package(s) pinned at their current version: the release "
        "they are on is itself inside the window, and the bound must never roll a "
        "dependency backwards",
        f"- {len(advanced)} package(s) upgraded, {len(added)} newly added — all of "
        f"them published at least `{args.window}` ago",
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
