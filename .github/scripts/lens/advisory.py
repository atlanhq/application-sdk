"""Advisory notes: reminders the summary shows but lens never raises as findings.

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


def notes(paths: list[str]) -> list[str]:
    """The advisory notes for a PR that changes `paths` (its whole diff against base)."""
    hits = [p for p in paths if any(glob_match(p, g) for g in SCAN_GLOBS)]
    if not hits:
        return []
    named = ", ".join(f"`{p}`" for p in hits[:_MAX_NAMED])
    more = f" and {len(hits) - _MAX_NAMED} more" if len(hits) > _MAX_NAMED else ""
    return [
        f"this PR changes dependencies or a Dockerfile ({named}{more}): run a trivy or "
        "grype scan and record the result in the description "
        "(`docs/standards/build-security.md`)."
    ]
