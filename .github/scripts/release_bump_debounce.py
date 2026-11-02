#!/usr/bin/env python3
"""Decide whether a freshly built bump commit needs to be pushed (FND-3322).

``bump-version-<target>`` is a fixed branch, and the release workflow used to
force-push it on *every* merge to the target. Each force-push is a
``synchronize`` on the open bump PR, which re-runs all of its PR CI —
conformance, tests, vulnerability scan, release gate — on a diff that is only a
version string and a changelog section. In a Renovate-automerge fleet almost all
of those merges change nothing a release would ship: Renovate commits are forced
to ``chore`` (``renovate-config/default.json``), so they never move the computed
version past the first patch bump. Since FND-3748 ``update_changelog.py`` renders
every conventional type except ``ci`` — chores included — in any repo whose
bump-PR CI is gated on the ``gate-check`` opt-in, so there a Renovate merge does
change the notes and does push (cheaply: nothing heavy runs until opted in).
An app repo that has not resynced onto the gated shims keeps the old Features
and Bug Fixes notes, so the reasoning above still holds for it.

So: push only when the open branch would release something different from what
this run built, and never from a run older than the branch. First the bases are
compared — the commit this run built on against the one the branch was built on
(the bump commit's parent) (FND-4488):

  * the branch does not contain this run's base — main moved under it: push;
  * the branch is built on a strictly newer base — this run is an older merge's
    run that arrived late, so whatever it computed is a subset of what the
    branch already releases: skip, whatever its version or notes say;
  * the same base: push only if, against ``origin/<branch>``, the version in
    some version file or the CHANGELOG section for that version (ignoring the
    date in its heading, which moves every day) differs.

The first rule keeps the branch's tree identical to what its merge ships.
``/gate-check`` tests the bump PR's *head*, not its merge ref, so a branch left
on an older base is gated on a tree that is not the one released: two Renovate
merges seconds apart (lock maintenance, then an application-sdk bump) queued two
runs, the first built on the older commit and pushed, and the second — built on
the commit carrying the new SDK — matched version and notes and skipped, so the
release gate tested the old SDK while the merge would ship the new one. The
second rule is what keeps a burst from moving the branch backwards. A branch
containing the base also merges onto it cleanly by construction, which is what
an older merge-tree check here used to establish.

Fails open: any check that cannot be made — the branch does not exist, a file is
missing on it, git errors — resolves to ``push=true``, which is the behaviour
before this script existed. Skipping a push wrongly ships stale notes; pushing
wrongly only costs a CI run.

Usage::

    python3 release_bump_debounce.py \
        --branch bump-version-main \
        --version-file pyproject.toml \
        --changelog CHANGELOG.md \
        --new-version 1.2.3

Writes ``push=<true|false>`` and ``reason=<text>`` to $GITHUB_OUTPUT (or prints
them when unset). Always exits 0; the workflow gates its push step on ``push``.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

# `version = "1.2.3"` in pyproject.toml, `__version__ = "1.2.3"` in version.py.
_VERSION_LINE_RE = re.compile(
    r"""^(?:version|__version__)\s*=\s*["']([^"']+)["']""", re.MULTILINE
)


#: A stalled git call must fail open (push) promptly, not hold the bump job
#: until its outer timeout.
GIT_TIMEOUT_S = 90


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Single seam so tests can stub git.

    A command that outlives ``GIT_TIMEOUT_S`` reads as a failure (exit 124),
    which every caller already treats as "cannot tell, push".
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=GIT_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 124, "", f"timed out after {GIT_TIMEOUT_S}s"
        )


def _git_show(ref: str, path: str) -> str | None:
    result = run(["git", "show", f"{ref}:{path}"])
    return result.stdout if result.returncode == 0 else None


def read_version(text: str) -> str | None:
    """Return the first top-level version assignment in ``text``.

    The first match is the right one for both supported files: pyproject.toml's
    ``[project]`` table precedes any tool table that might also say
    ``version =``, and version.py has a single ``__version__``.
    """
    match = _VERSION_LINE_RE.search(text)
    return match.group(1) if match else None


def changelog_section(text: str, version: str) -> str | None:
    """Return the ``## v<version>`` section with its heading date removed.

    Same section boundary as ``extract_release_notes.py`` — the heading up to
    the next ``## v`` — so "the notes" here means exactly what tag-and-release
    would publish, minus the date.
    """
    pattern = rf"^## v{re.escape(version)} \([^)]*\)(.*?)(?=^## v|\Z)"
    match = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return match.group(1).strip() if match else None


def rev_parse(ref: str) -> str | None:
    """The full SHA ``ref`` names, or None when git cannot resolve it."""
    result = run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return result.stdout.strip() if result.returncode == 0 else None


def contains(ref: str, commit: str) -> bool:
    """True when ``commit`` is ``ref`` or one of its ancestors.

    ``git merge-base --is-ancestor`` exits 0 for yes and 1 for no; any other
    outcome (an unknown commit, a git error) counts as "no", which pushes.
    """
    return run(["git", "merge-base", "--is-ancestor", commit, ref]).returncode == 0


def decide(
    *,
    branch: str,
    version_files: list[str],
    changelog: str,
    new_version: str,
    workdir: Path,
    onto: str = "HEAD",
) -> tuple[bool, str]:
    """Return ``(push, reason)`` for the bump commit built in ``workdir``."""
    remote = f"origin/{branch}"
    fetch = run(
        [
            "git",
            "fetch",
            "--no-tags",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/{remote}",
        ]
    )
    if fetch.returncode != 0:
        return True, f"{remote} could not be fetched (first bump, or deleted)"

    built_on = rev_parse(onto)
    branch_base = rev_parse(f"{remote}^")
    if built_on is None or branch_base is None:
        return True, f"the bases of {onto} and {remote} could not be resolved"
    if not contains(remote, built_on):
        return True, (
            f"{remote} is built on an older base than this run's "
            f"{built_on[:12]} — rebuilding so the gated head is what the merge ships"
        )
    if branch_base != built_on:
        return False, (
            f"{remote} is built on {branch_base[:12]}, newer than this run's "
            f"{built_on[:12]} — a late run for an older merge, not re-pushing"
        )

    for path in version_files:
        published = _git_show(remote, path)
        if published is None:
            return True, f"{path} is missing on {remote}"
        if read_version(published) != new_version:
            return True, (
                f"version changed: {remote} has {read_version(published)}, "
                f"this run computed {new_version}"
            )

    local_changelog = workdir / changelog
    if not local_changelog.is_file():
        return True, f"{changelog} was not generated by this run"
    built = changelog_section(local_changelog.read_text(encoding="utf-8"), new_version)
    published_changelog = _git_show(remote, changelog)
    published = (
        changelog_section(published_changelog, new_version)
        if published_changelog is not None
        else None
    )
    if built is None or published is None or built != published:
        return True, f"release notes for v{new_version} changed"

    return False, (
        f"{remote} already carries v{new_version} with the same release notes "
        f"on this run's base {built_on[:12]} — not re-pushing"
    )


def _set_output(key: str, value: str) -> None:
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"{key}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--branch", required=True, help="Fixed bump branch name")
    parser.add_argument(
        "--version-file",
        action="append",
        required=True,
        help="File carrying the version; repeat for several",
    )
    parser.add_argument("--changelog", default="CHANGELOG.md")
    parser.add_argument("--new-version", required=True)
    args = parser.parse_args(argv)

    try:
        push, reason = decide(
            branch=args.branch,
            version_files=args.version_file,
            changelog=args.changelog,
            new_version=args.new_version,
            workdir=Path.cwd(),
        )
    except Exception as exc:  # fail open: an unanswerable check means push
        push, reason = True, f"debounce check failed ({type(exc).__name__}: {exc})"

    print(f"{'Pushing' if push else 'Skipping push'}: {reason}")
    _set_output("push", "true" if push else "false")
    _set_output("reason", reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
