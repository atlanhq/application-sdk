"""Suppression-directive parsing for TOML text (``pyproject.toml``).

The AST-based ``_directives`` parser tokenizes *Python* source, so it can't be
reused as-is for TOML files (``tomllib`` discards comments entirely, and
Python's tokenizer rejects TOML syntax). TOML uses ``#`` for comments too, so
the same ``# conformance: ignore[...]`` directive grammar applies — this
module implements the line-scanning half for TOML text, shared by every
series with pyproject.toml-anchored findings (D-series, T010-T012/T014-T015).
"""

from __future__ import annotations

import re

from conformance.suite.schema.findings import Finding

__all__ = [
    "SuppressionsMap",
    "_is_suppressed",
    "make_toml_finding",
    "parse_toml_suppressions",
]

# Identical grammar to the AST-series ``_directives._SUPPRESS_RE`` — kept as a
# separate constant because it is matched against a raw comment substring
# found by line-scanning, not against a tokenizer-emitted COMMENT token.
_SUPPRESS_RE = re.compile(
    r"^#\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)

# A single bracket entry: ``T025`` (rule-wide) or ``T025:miner`` (one subject).
# The ``:discriminator`` suffix lets a rule that emits several findings at one
# location (see ``Finding.discriminator``) be suppressed per subject rather
# than all-or-nothing.
_RULE_ID_RE = re.compile(r"^(?P<rule>[A-Za-z0-9]+)(?::(?P<discriminator>[^,]+))?$")

# ``{lineno: (rule_ids_or_None, discriminators, justification)}``:
#   * rule_ids None            → directive matches any rule on that line;
#   * discriminators None      → no ``:subject`` suffixes were used, so every
#                                listed rule suppresses rule-wide;
#   * discriminators {rule: {subjects}} → for those rules, only a finding whose
#                                own discriminator is named suppresses. A rule
#                                listed both bare and with a subject is
#                                rule-wide (the bare form wins).
SuppressionsMap = dict[
    int, tuple[frozenset[str] | None, dict[str, frozenset[str]] | None, str]
]


def parse_toml_suppressions(text: str) -> SuppressionsMap:
    """Return the suppression directives in *text*, keyed by line number.

    ``rule_ids_or_None`` is ``None`` for a rule-id-less directive (matches any
    rule on that line). A directive at line N suppresses findings on lines N
    and N+1 (its own line and the one immediately below), mirroring the
    AST-series convention so users only have to learn one form. Bare
    directives with no justification text are rejected — unexplained
    suppressions carry no audit value.

    An entry may carry a subject: ``# conformance: ignore[T025:miner] <why>``
    suppresses only findings whose own discriminator is ``miner`` (subject
    matching is case-insensitive); ``# conformance: ignore[T025] <why>`` still
    suppresses every T025 finding on the line.
    """
    out: SuppressionsMap = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        idx = raw.lstrip().find("#")
        if idx == -1:
            continue
        comment = raw.lstrip()[idx:]
        m = _SUPPRESS_RE.match(comment)
        if m is None:
            continue
        justification = (m.group(2) or "").strip()
        if not justification:
            continue
        ids_blob = (m.group(1) or "").strip()
        rule_ids: set[str] = set()
        discriminators: dict[str, set[str]] = {}
        saw_discriminator = False
        for entry in (s.strip() for s in ids_blob.split(",") if s.strip()):
            em = _RULE_ID_RE.match(entry)
            if em is None:
                continue
            rule = em.group("rule").upper()
            subject = em.group("discriminator")
            if subject is None:
                rule_ids.add(rule)
                # The bare form is rule-wide; a ``:subject`` entry for the same
                # rule elsewhere in the list must not narrow it back.
                discriminators.pop(rule, None)
            elif rule not in rule_ids:
                saw_discriminator = True
                discriminators.setdefault(rule, set()).add(subject.strip().lower())
        out[lineno] = (
            None if not ids_blob else frozenset(rule_ids) | frozenset(discriminators),
            (
                {r: frozenset(s) for r, s in discriminators.items()}
                if saw_discriminator
                else None
            ),
            justification,
        )
    return out


def _is_suppressed(
    suppressions: SuppressionsMap,
    rule_id: str,
    line: int,
    discriminator: str | None = None,
) -> tuple[bool, str | None]:
    for cand in (line, line - 1):
        if cand not in suppressions:
            continue
        rule_ids, discriminators, justification = suppressions[cand]
        if rule_ids is None:
            return True, justification
        if rule_id not in rule_ids:
            continue
        if discriminators is None or rule_id not in discriminators:
            # The rule was listed bare (no ``:subject``) — rule-wide.
            return True, justification
        if discriminator is not None and discriminator.lower() in (
            discriminators[rule_id] or ()
        ):
            return True, justification
    return False, None


def make_toml_finding(
    *,
    rule_id: str,
    file: str,
    line: int,
    column: int,
    message: str,
    suppressions: SuppressionsMap,
    discriminator: str | None = None,
) -> Finding:
    """Build a :class:`Finding` anchored in TOML text, honouring suppressions."""
    suppressed, justification = _is_suppressed(
        suppressions, rule_id, line, discriminator
    )
    return Finding(
        rule_id=rule_id,
        file=file,
        line=line,
        column=column,
        message=message,
        discriminator=discriminator,
        suppressed=suppressed,
        suppression_justification=justification,
    )
