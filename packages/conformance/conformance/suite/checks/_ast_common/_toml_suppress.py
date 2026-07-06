"""Suppression-directive parsing for TOML text (``pyproject.toml``).

The AST-based ``_directives`` parser tokenizes *Python* source, so it can't be
reused as-is for TOML files (``tomllib`` discards comments entirely, and
Python's tokenizer rejects TOML syntax). TOML uses ``#`` for comments too, so
the same ``# conformance: ignore[...]`` directive grammar applies — this
module re-implements just the line-scanning half for TOML text, following the
precedent already established in ``dependency_conformance`` (D-series) for
its own pyproject.toml-anchored findings.
"""

from __future__ import annotations

import re

from conformance.suite.schema.findings import Finding

__all__ = ["make_toml_finding", "parse_toml_suppressions"]

# Identical grammar to the AST-series ``_directives._SUPPRESS_RE`` — kept as a
# separate constant because it is matched against a raw comment substring
# found by line-scanning, not against a tokenizer-emitted COMMENT token.
_SUPPRESS_RE = re.compile(
    r"^#\s*conformance\s*:\s*ignore\s*(?:\[([^\]]*)\])?\s*(.*)",
    re.IGNORECASE,
)


def parse_toml_suppressions(text: str) -> dict[int, tuple[frozenset[str] | None, str]]:
    """Return ``{lineno: (rule_ids_or_None, justification)}`` for *text*.

    ``rule_ids_or_None`` is ``None`` for a rule-id-less directive (matches any
    rule on that line). A directive at line N suppresses findings on lines N
    and N+1 (its own line and the one immediately below), mirroring the
    AST-series convention so users only have to learn one form. Bare
    directives with no justification text are rejected — unexplained
    suppressions carry no audit value.
    """
    out: dict[int, tuple[frozenset[str] | None, str]] = {}
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
        rule_ids: frozenset[str] | None = (
            None
            if not ids_blob
            else frozenset(s.strip().upper() for s in ids_blob.split(",") if s.strip())
        )
        out[lineno] = (rule_ids, justification)
    return out


def _is_suppressed(
    suppressions: dict[int, tuple[frozenset[str] | None, str]],
    rule_id: str,
    line: int,
) -> tuple[bool, str | None]:
    for cand in (line, line - 1):
        if cand not in suppressions:
            continue
        rule_ids, justification = suppressions[cand]
        if rule_ids is None or rule_id in rule_ids:
            return True, justification
    return False, None


def make_toml_finding(
    *,
    rule_id: str,
    file: str,
    line: int,
    column: int,
    message: str,
    suppressions: dict[int, tuple[frozenset[str] | None, str]],
) -> Finding:
    """Build a :class:`Finding` anchored in TOML text, honouring suppressions."""
    suppressed, justification = _is_suppressed(suppressions, rule_id, line)
    return Finding(
        rule_id=rule_id,
        file=file,
        line=line,
        column=column,
        message=message,
        suppressed=suppressed,
        suppression_justification=justification,
    )
