"""Advisory notes: reminders the summary shows but the reviewer never raises as findings.

A finding must be closable by a code change, because verify judges only code:
the round's diff and the code at the finding's site, never the PR description.
Some rules ask for something only the description or an outside action can
supply, so no commit can close a finding for one, and it stays open until
someone dismisses it.

Such a rule is an advisory instead: decided here, in code, from the PR's
changed paths; stored in the PR state so every re-render keeps it; shown in
the summary; never fingerprinted, verified, counted or blocking.

This fork ships none. lens has one, a trivy/grype scan reminder that cites an
application-sdk doc, and it does not apply to an app repo. `ADVISORIES` is the
extension point: add an `Advisory` there and every PR whose changed paths
match one of its globs carries its note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .diff import FileDiff
from .rules import glob_match

_MAX_NAMED = 3
_PATHS = "{paths}"


@dataclass(frozen=True)
class Advisory:
    """One reminder: which changed paths raise it, and what it says.

    `{paths}` in `note` is replaced by the matching paths: at most `_MAX_NAMED`
    of them, each as a code span, then "and N more"."""

    globs: tuple[str, ...]
    note: str


# None yet: see the module docstring.
ADVISORIES: tuple[Advisory, ...] = ()


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
    in the reviewer's own summary: the fence is longer than any backtick run inside,
    and line breaks (which would end the line the note sits on) become spaces."""
    path = re.sub(r"[\r\n]+", " ", path)
    runs = [len(m) for m in re.findall(r"`+", path)]
    if not runs:
        return f"`{path}`"
    fence = "`" * (max(runs) + 1)
    return f"{fence} {path} {fence}"


def notes(
    paths: list[str], advisories: tuple[Advisory, ...] | None = None
) -> list[str]:
    """The advisory notes for a PR that changes `paths` (its whole diff against base,
    from `changed_paths`), one per advisory that matches. `advisories` defaults to
    `ADVISORIES`, read when called."""
    out: list[str] = []
    for adv in ADVISORIES if advisories is None else advisories:
        hits = [p for p in paths if any(glob_match(p, g) for g in adv.globs)]
        if not hits:
            continue
        named = ", ".join(_code(p) for p in hits[:_MAX_NAMED])
        more = f" and {len(hits) - _MAX_NAMED} more" if len(hits) > _MAX_NAMED else ""
        out.append(adv.note.replace(_PATHS, f"{named}{more}"))
    return out
