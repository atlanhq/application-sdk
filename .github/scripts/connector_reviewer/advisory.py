"""Advisory notes: reminders the summary shows but connector-reviewer never raises as findings.

A finding must be closable by a code change, because verify judges only code:
the round's diff and the code at the finding's site, never the PR description.
Some rules ask for something only the description or an outside action can
supply — "run a trivy/grype scan and record it" — so no commit can close a
finding for one, and it stays open until someone dismisses it.

Such a rule is an advisory instead: decided here, in code, from the PR's
changed paths; stored in the PR state so every re-render keeps it; shown in
the summary; never fingerprinted, verified, counted or blocking.
"""

from __future__ import annotations

import re

from .diff import FileDiff
from .rules import glob_match

# Dockerfiles and dependency manifests/lockfiles (`docs/standards/build-security.md`).
SCAN_GLOBS = (
    "**/Dockerfile*",
    "**/*.Dockerfile",
    "**/pyproject.toml",
    "**/uv.lock",
    "**/requirements*.txt",
    "**/package.json",
    "**/package-lock.json",
)
_MAX_NAMED = 3


def changed_paths(files: list[FileDiff]) -> list[str]:
    """Every path a diff touches, a rename's former name included: renaming a
    Dockerfile away still changes the Dockerfile."""
    out: list[str] = []
    for fd in files:
        for p in (fd.old_path, fd.path):
            if p and p not in out:
                out.append(p)
    return out


def _code(path: str) -> str:
    """`path` as a Markdown code span it cannot break out of. Paths come from the PR,
    so a backtick in one must not close the span and let the rest render as Markdown
    in connector-reviewer's own summary: the fence is longer than any backtick run inside, and line
    breaks (which would end the line the note sits on) become spaces."""
    path = re.sub(r"[\r\n]+", " ", path)
    runs = [len(m) for m in re.findall(r"`+", path)]
    if not runs:
        return f"`{path}`"
    fence = "`" * (max(runs) + 1)
    return f"{fence} {path} {fence}"


def notes(paths: list[str]) -> list[str]:
    """The advisory notes for a PR that changes `paths` (its whole diff against base,
    from `changed_paths`)."""
    hits = [p for p in paths if any(glob_match(p, g) for g in SCAN_GLOBS)]
    if not hits:
        return []
    named = ", ".join(_code(p) for p in hits[:_MAX_NAMED])
    more = f" and {len(hits) - _MAX_NAMED} more" if len(hits) > _MAX_NAMED else ""
    return [
        f"this PR changes dependencies or a Dockerfile ({named}{more}): run a trivy or "
        "grype scan and record the result in the description "
        "(`docs/standards/build-security.md`)."
    ]
