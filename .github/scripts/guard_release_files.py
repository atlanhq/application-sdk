#!/usr/bin/env python3
"""Release-owned file guard.

The version and CHANGELOG of each independently-published package in this repo
are owned *exclusively* by that package's automated release flow, which commits
them on a dedicated ``bump-version-*`` branch:

  * conformance       -> bump-version-conformance
                         (packages/conformance/pyproject.toml,
                          packages/conformance/conformance/__init__.py,
                          packages/conformance/CHANGELOG.md)
  * contract-toolkit  -> bump-version-contract-toolkit
                         (contract-toolkit/src/PklProject,
                          contract-toolkit/CHANGELOG.md)

A human editing any of these files on a normal branch desyncs the package from
its published state and wedges the release automation. The motivating incident:
a feature PR hand-bumped the conformance version to a value that was never
tagged/published, and conformance_release.py hard-errors when the declared
version has no matching ``conformance-v*`` tag -- so *every* later conformance
merge failed and nothing reached the changelog. A second PR hand-edited the
conformance CHANGELOG's ``[Unreleased]`` section, which fights the generator's
prepend.

This guard fails a PR that, off the owning ``bump-version-*`` branch:
  * changes a package's version line, or
  * touches a package's CHANGELOG at all.

The SDK itself is intentionally NOT guarded here: its release flow
(.github/scripts/release.py) derives the previous version from ``git describe``
rather than requiring the declared version to have a matching tag, so a stray
manual edit degrades gracefully instead of wedging the pipeline.

Usage:
    python3 guard_release_files.py \
        --diff <path to a unified diff of the PR> \
        --head-ref <PR head branch> \
        --comment-out <path to write the sticky-comment markdown on violation>

Writes ``violation`` and ``error_message`` to $GITHUB_OUTPUT (or prints
``key=value`` lines if unset). Always exits 0 -- the calling workflow gates on
the ``violation`` output in a separate step, mirroring pr_title_convention.py.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field

# `diff --git a/<path> b/<path>` -- group 2 is the post-image path, which is
# the file's name for edits and renames alike (deletions set it to /dev/null,
# which never matches a guarded path).
DIFF_GIT_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$")

# Version-line matchers, keyed by the file they apply to. `version =` in a
# pyproject [project] table is unindented; the toolkit's PklProject nests it
# under the package block, so it is indented.
_PYPROJECT_VERSION = re.compile(r"^version\s*=")
_PY_DUNDER_VERSION = re.compile(r"^__version__\s*=")
_PKL_VERSION = re.compile(r"^\s+version\s*=")


@dataclass(frozen=True)
class Package:
    name: str
    bump_branch: str
    version_files: dict  # path -> compiled regex matching its version line
    changelog_files: tuple = field(default_factory=tuple)

    def owns_branch(self, head_ref: str) -> bool:
        """True when ``head_ref`` is this package's release branch."""
        return head_ref == self.bump_branch or head_ref.startswith(
            self.bump_branch + "-"
        )


PACKAGES = (
    Package(
        name="conformance",
        bump_branch="bump-version-conformance",
        version_files={
            "packages/conformance/pyproject.toml": _PYPROJECT_VERSION,
            "packages/conformance/conformance/__init__.py": _PY_DUNDER_VERSION,
        },
        changelog_files=("packages/conformance/CHANGELOG.md",),
    ),
    Package(
        name="contract-toolkit",
        bump_branch="bump-version-contract-toolkit",
        version_files={
            "contract-toolkit/src/PklProject": _PKL_VERSION,
        },
        changelog_files=("contract-toolkit/CHANGELOG.md",),
    ),
)


def parse_diff(diff_text: str) -> dict:
    """Map each file in a unified diff to its changed content lines.

    A changed line is an added/removed line with its leading ``+``/``-``
    stripped; the ``+++``/``---`` file headers are skipped. A file that appears
    in the diff is always a key (even with an empty list), so callers can detect
    "this file was touched at all" independently of *what* changed.
    """
    files: dict = {}
    current = None
    for line in diff_text.splitlines():
        m = DIFF_GIT_RE.match(line)
        if m:
            current = m.group(2).strip()
            files.setdefault(current, [])
            continue
        if current is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            files[current].append(line[1:])
    return files


def evaluate(changed: dict, head_ref: str) -> list:
    """Return a list of (path, reason) violations for a parsed diff.

    ``changed`` is the mapping from :func:`parse_diff`; ``head_ref`` is the PR
    head branch.
    """
    violations = []
    for pkg in PACKAGES:
        if pkg.owns_branch(head_ref):
            continue
        for path, version_re in pkg.version_files.items():
            lines = changed.get(path)
            if lines and any(version_re.match(line) for line in lines):
                violations.append((path, f"{pkg.name} version line"))
        for path in pkg.changelog_files:
            if path in changed:
                violations.append((path, f"{pkg.name} changelog"))
    return violations


_COMMENT = """\
### \U0001f512 Release-owned files can only change on the release branch

This PR edits a version or `CHANGELOG` that is owned by an automated release
flow, and it is not on that package's `bump-version-*` branch:

{items}

These files are generated and committed **only** by the release automation
(`.github/workflows/*-release.yml`, which pushes the `bump-version-*` branch).
Hand-editing them on a feature branch desyncs the package from its published
tags and wedges the release pipeline.

**What to do instead:**
- Version bump / release: let it happen automatically. Merging a normal change
  under the package opens (or updates) the release PR, and merging *that* PR
  publishes and tags. You never edit the version by hand.
- Changelog: it is regenerated from your commit subjects on release — just write
  a clear conventional-commit PR title; don't edit `CHANGELOG.md` directly.

If you are doing one-off incident recovery on these files (e.g. reverting an
unpublished bump), that correction must land **before** this guard becomes
required, or a repo admin must override the check — by design there is no
per-PR bypass.

Pushing a commit that drops these file changes re-runs this check and clears
this comment automatically.
"""

_ERROR_MESSAGE = (
    "Release-owned files (package version / CHANGELOG) may only be changed on "
    "the owning bump-version-* branch by the release automation: {paths}"
)


def _write_output(key: str, value: str) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}"
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(line + "\n")
    else:
        print(line)


def run(diff_path: str, head_ref: str, comment_out_path: str):
    """Core decision logic, returned as (violation, violations) for testing."""
    with open(diff_path) as fh:
        diff_text = fh.read()

    changed = parse_diff(diff_text)
    violations = evaluate(changed, head_ref)

    if not violations:
        print(f"No release-owned files changed off a bump branch ({head_ref}). ✅")
        return False, violations

    for path, reason in violations:
        print(f"Violation: {path} ({reason})")

    items = "\n".join(f"- `{path}` — {reason}" for path, reason in violations)
    with open(comment_out_path, "w") as fh:
        fh.write(_COMMENT.format(items=items))

    return True, violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True, help="Path to the PR unified diff")
    parser.add_argument("--head-ref", default="")
    parser.add_argument(
        "--comment-out",
        required=True,
        help="Path to write the sticky-comment markdown when the PR violates the guard",
    )
    args = parser.parse_args()

    print(f"Head branch: {args.head_ref}")
    violation, violations = run(args.diff, args.head_ref, args.comment_out)

    _write_output("violation", "true" if violation else "false")
    _write_output(
        "error_message",
        _ERROR_MESSAGE.format(paths=", ".join(p for p, _ in violations))
        if violation
        else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
