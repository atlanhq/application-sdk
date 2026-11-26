"""Lint the CHANGELOG.md section a release just generated.

Run by ``release.yaml`` after ``update_changelog.py`` and before the bump branch
is pushed, so a malformed section never reaches the bump PR, the GitHub release
body (``extract_release_notes.py``) or the Linear release notes. Three rules,
each one a defect the changelog has shipped before:

  * the ``## v<version>`` heading appears exactly once — ``v3.0.0`` has two
    sections, and the extractor can only ever publish the first;
  * the section has at least one bullet — four pre-v3 sections are empty;
  * every bullet carries a ``(#N)`` pull-request reference — the SDK merges
    through squash-merged PRs, so a bullet without one is a commit that reached
    main some other way, and its notes cannot be traced to a review.

Only the named version's section is checked. Older sections predate the lint,
and the changelog is never edited by hand to fix them: the major-bump archive
in ``update_changelog.py`` moves them out of CHANGELOG.md instead.

Usage: python lint_changelog.py <version> [--changelog CHANGELOG.md]
Exits 1 and prints one ``::error::`` line per problem when the section fails.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from _changelog import sections_for

# `(#5073)`, as GitHub appends to every squash-merge subject.
PR_REF_RE = re.compile(r"\(#\d+\)")


def lint(text: str, version: str) -> list[str]:
    """Return one message per problem in ``version``'s section; [] when clean."""
    version = version.removeprefix("v")
    sections = sections_for(text, version)
    if not sections:
        return [f"no `## v{version} (<date>)` section in the changelog"]

    problems: list[str] = []
    if len(sections) > 1:
        lines = ", ".join(str(s.line) for s in sections)
        problems.append(
            f"`## v{version}` appears {len(sections)} times (lines {lines})"
        )

    section = sections[0]
    bullets = section.bullets
    if not bullets:
        problems.append(
            f"`## v{version}` (line {section.line}) has no bullets: every commit "
            "since the last release was filtered out (e.g. all `ci:`), so this "
            "release would ship with empty notes"
        )
    for bullet in bullets:
        if not PR_REF_RE.search(bullet):
            problems.append(
                f"`## v{version}` has a bullet with no `(#N)` PR reference: {bullet}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint the CHANGELOG.md section a release just generated."
    )
    parser.add_argument("version", help="The version whose section to lint.")
    parser.add_argument("--changelog", default="CHANGELOG.md", type=Path)
    args = parser.parse_args(argv)

    problems = lint(args.changelog.read_text(encoding="utf-8"), args.version)
    for problem in problems:
        print(f"::error file={args.changelog}::{problem}")
    if problems:
        return 1
    print(f"{args.changelog}: v{args.version.removeprefix('v')} section is clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
