#!/usr/bin/env python3
"""Decide whether a freshly built bump commit needs to be pushed (FND-3322).

``bump-version-<target>`` is a fixed branch, and the release workflow used to
force-push it on *every* merge to the target. Each force-push is a
``synchronize`` on the open bump PR, which re-runs all of its PR CI —
conformance, tests, vulnerability scan, release gate — on a diff that is only a
version string and a changelog section. In a Renovate-automerge fleet almost all
of those merges change nothing a release would ship: Renovate commits are forced
to ``chore`` (``renovate-config/default.json``), so they never move the computed
version past the first patch bump, and ``update_changelog.py`` renders only the
Features and Bug Fixes sections, so they never change the notes either.

So: push only when the open branch would release something different from what
this run built. The branch is left alone when, against what is already on
``origin/<branch>``, all of these hold:

  * the version in each version file is the same;
  * the CHANGELOG section for that version is the same, ignoring the date in
    its heading (the date moves every day and is not release content);
  * the branch still merges cleanly onto the commit this run built on — a
    branch that went CONFLICTING must be rebuilt, or the release wedges.

Not pushing leaves the branch on an older base, which is safe: it only edits
the version line(s), ``uv.lock``'s own-package version and the top of
``CHANGELOG.md``, and the merge (and the e2e run on the ``e2e`` label, which
tests the PR's merge ref) combines that with the current target. The
mergeability check is what catches the case where it would not.

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


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Single seam so tests can stub git."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


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


def merges_cleanly(onto: str, ref: str) -> bool:
    """True when ``ref`` merges onto ``onto`` without conflicts.

    ``git merge-tree --write-tree`` (git >= 2.38) exits 0 on a clean merge and
    1 on conflicts without touching the worktree or index. Any other outcome,
    including an old git that does not know the flag, counts as "not clean".
    """
    return run(["git", "merge-tree", "--write-tree", onto, ref]).returncode == 0


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

    if not merges_cleanly(onto, remote):
        return True, f"{remote} no longer merges cleanly onto {onto}"

    return False, (
        f"{remote} already carries v{new_version} with the same release notes "
        "and still merges cleanly — not re-pushing"
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
