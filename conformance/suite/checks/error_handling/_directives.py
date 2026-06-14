"""Directive parsing — ``# conformance: ignore[...]`` and ``# noqa`` shorthands."""

from __future__ import annotations

import io
import tokenize
from dataclasses import dataclass

from ._constants import _NOQA_RE, _NOQA_TO_RULES, _SUPPRESS_RE


@dataclass(frozen=True)
class _IgnoreDirective:
    """Parsed ``# conformance: ignore[...]`` directive."""

    rule_ids: frozenset[str] | None  # None = suppress every rule on this line
    justification: str
    # True when the directive appears on a comment-only line (no preceding code
    # token on the same row).  Used by Checker._add to decide whether a
    # "line above" directive is applicable: trailing inline directives on code
    # lines must not absorb findings on the *next* statement.
    comment_only: bool = True


def _parse_directives(source: str) -> dict[int, _IgnoreDirective]:
    """Return ``{lineno: directive}`` for all suppression comments.

    Recognises two forms:

    * ``# conformance: ignore[E001,E002] reason`` — explicit conformance directive
    * ``# noqa: S110 — reason`` — noqa shorthand when a mapped code + justification
      text are both present (bare ``# noqa`` and ``# noqa: CODE`` without text are
      rejected; unknown codes produce no suppression)
    """
    directives: dict[int, _IgnoreDirective] = {}
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        # Tokenisation fails on source with unclosed string literals or other
        # lexical errors.  All suppression directives in the file are silently
        # lost, which means findings that would have been suppressed will be
        # reported.  This is intentionally conservative: false positives are
        # preferable to silently dropping findings on malformed source.  In
        # practice scan_text() returns early on SyntaxError before this path
        # is reached, so the window is narrow.
        return directives

    # Identify lines that carry non-comment code tokens so we can mark inline
    # trailing directives (e.g. ``do_it()  # conformance: ignore[E001]``) as
    # not comment-only.  These tokens are not meaningful on their own.
    _SKIP_TYPES = frozenset(
        {
            tokenize.COMMENT,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.ENDMARKER,
            tokenize.ENCODING,
        }
    )
    code_lines: set[int] = {
        srow for tok_type, _, (srow, _scol), *_ in tokens if tok_type not in _SKIP_TYPES
    }

    for tok_type, tok_string, (srow, _), *_ in tokens:
        if tok_type != tokenize.COMMENT:
            continue

        # ── conformance: ignore[...] ──────────────────────────────────────────
        m = _SUPPRESS_RE.search(tok_string)
        if m:
            raw_ids, justification = m.group(1), (m.group(2) or "").strip()
            rule_ids: frozenset[str] | None
            if raw_ids:
                rule_ids = frozenset(
                    r.strip().upper() for r in raw_ids.split(",") if r.strip()
                )
            else:
                rule_ids = None
            directives[srow] = _IgnoreDirective(
                rule_ids=rule_ids,
                justification=justification,
                comment_only=srow not in code_lines,
            )
            continue

        # ── # noqa: CODE — justification ─────────────────────────────────────
        m = _NOQA_RE.search(tok_string)
        if not m:
            continue
        justification = m.group(2).strip()
        mapped: set[str] = set()
        for code in (c.strip().upper() for c in m.group(1).split(",")):
            if code in _NOQA_TO_RULES:
                mapped.update(_NOQA_TO_RULES[code])
        if not mapped:
            continue  # all codes unknown — no suppression
        directives[srow] = _IgnoreDirective(
            rule_ids=frozenset(mapped),
            justification=justification,
            comment_only=srow not in code_lines,
        )
    return directives


def parse_ignore_directive(comment: str) -> _IgnoreDirective | None:
    """Parse a raw comment string. Returns None if it is not a conformance directive."""
    m = _SUPPRESS_RE.search(comment)
    if not m:
        return None
    raw_ids, justification = m.group(1), (m.group(2) or "").strip()
    rule_ids: frozenset[str] | None
    if raw_ids:
        rule_ids = frozenset(r.strip().upper() for r in raw_ids.split(",") if r.strip())
    else:
        rule_ids = None
    return _IgnoreDirective(rule_ids=rule_ids, justification=justification)
