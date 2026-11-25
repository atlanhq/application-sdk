"""Pkl-source suppression directive parser for ``// conformance: ignore[...]``.

Pkl files use ``//`` for line comments (Python's ``#`` is not a pkl comment), so
the Python-tokenise-based ``_ast_common._directives`` module cannot be reused
here.  This module provides an equivalent directive parser for ``.pkl`` source.

Grammar
-------
The directive form is identical to the Python form, only the comment prefix
differs:

    // conformance: ignore[K001,K002] this contract intentionally uses NativeApp.pkl

Rule ids in the bracket list are optional (omitting them suppresses every rule
on that line).  Justification text after the bracket (or after the colon when
there are no brackets) is **mandatory** — bare directives with no text are
silently rejected so findings are not suppressed by accident.

A directive applies to the **violating line** or to the **comment-only line
directly above** it (same semantics as the Python form; the latter is useful
when the violating line itself has no room for a trailing comment, or when the
amends line spans column 0).

Block comments (``/* … */``) are not currently parsed for directives; only
line comments that match the ``// conformance: ignore`` pattern are considered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches: // conformance: ignore[K001,K002] justification text
# Groups: (1) optional comma-separated rule ids, (2) justification
_SUPPRESS_RE = re.compile(
    r"//\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _PklIgnoreDirective:
    """Parsed ``// conformance: ignore[...]`` directive from a pkl source file."""

    rule_ids: frozenset[str] | None
    """Rule IDs the directive suppresses, or ``None`` to suppress every rule."""

    justification: str
    """Mandatory explanation; directives with no text are silently rejected."""

    comment_only: bool = True
    """True when the directive appears on a line that contains no non-comment
    content before the ``//``.  A trailing inline directive on a code line never
    absorbs a finding on the *next* line — mirrors the Python ``make_finding``
    logic exactly."""


def _parse_pkl_directives(source: str) -> dict[int, _PklIgnoreDirective]:
    """Return ``{1-based-lineno: directive}`` for all ``// conformance: ignore``
    comments in *source*.

    Lines that have a directive but no justification text are silently excluded
    (same conservative policy as the Python ``_parse_directives``).
    """
    directives: dict[int, _PklIgnoreDirective] = {}
    for lineno, raw_line in enumerate(source.splitlines(), start=1):
        # Determine whether this line is comment-only (no non-whitespace content
        # before the ``//``).  A line like ``  amends "…" // conformance: …``
        # is NOT comment-only; a line like ``  // conformance: …`` IS.
        stripped = raw_line.lstrip()
        comment_only = stripped.startswith("//")

        m = _SUPPRESS_RE.search(raw_line)
        if not m:
            continue

        raw_ids, justification = m.group(1), (m.group(2) or "").strip()
        # Justification is mandatory — bare directives are silently rejected.
        if not justification:
            continue

        rule_ids: frozenset[str] | None
        if raw_ids:
            rule_ids = frozenset(
                r.strip().upper() for r in raw_ids.split(",") if r.strip()
            )
        else:
            rule_ids = None

        directives[lineno] = _PklIgnoreDirective(
            rule_ids=rule_ids,
            justification=justification,
            comment_only=comment_only,
        )

    return directives


def _make_pkl_finding_suppressed(
    *,
    rule_id: str,
    line: int,
    directives: dict[int, _PklIgnoreDirective],
) -> tuple[bool, str | None]:
    """Return ``(suppressed, justification)`` for a finding at *line*.

    Checks the finding's own line first, then the line directly above it (only
    when that prior line is comment-only — trailing inline directives on code
    lines must not absorb findings on the following line).
    """
    for check_line in (line, line - 1):
        if check_line not in directives:
            continue
        d = directives[check_line]
        if check_line == line - 1 and not d.comment_only:
            # Inline trailing directive on a code line — does not absorb the
            # finding on the next line.
            continue
        if d.rule_ids is None or rule_id in d.rule_ids:
            return True, d.justification
    return False, None
