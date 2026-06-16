"""Shared helpers for AST-based check modules (P-series, O-series, …).

The Python-source discovery walk, the ``# conformance: ignore[...]`` directive
parser, and the ``_get_name`` AST helper live in the E-series check package
(``checks.error_handling``); this module adds the one piece every AST series
needs but the E-series keeps private — suppression-aware ``Finding``
construction — so the P/O series do not each re-implement it.
"""

from __future__ import annotations

import ast

from conformance.suite.checks.error_handling._directives import _IgnoreDirective
from conformance.suite.schema.findings import Finding


def make_finding(
    *,
    filename: str,
    rule_id: str,
    node: ast.AST,
    message: str,
    directives: dict[int, _IgnoreDirective],
) -> Finding:
    """Build a :class:`Finding` for *node*, honouring inline suppression directives.

    A directive on the violating line — or on the *comment-only* line directly
    above it — suppresses the finding when it names *rule_id* (or is a wildcard
    ``# conformance: ignore`` with no rule list).  A trailing inline directive on
    a code line never absorbs a finding on the following statement.  Mirrors the
    E-series ``Checker._add`` semantics exactly.
    """
    line: int = getattr(node, "lineno", 1)
    col: int = getattr(node, "col_offset", 0) + 1
    suppressed = False
    justification: str | None = None
    for check_line in (line, line - 1):
        if check_line in directives:
            d = directives[check_line]
            if check_line == line - 1 and not d.comment_only:
                continue
            if d.rule_ids is None or rule_id in d.rule_ids:
                suppressed = True
                justification = d.justification
                break
    return Finding(
        rule_id=rule_id,
        file=filename,
        line=line,
        column=col,
        message=message,
        suppressed=suppressed,
        suppression_justification=justification,
    )
