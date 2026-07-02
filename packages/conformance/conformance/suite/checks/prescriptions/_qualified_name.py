"""P028 ManualQualifiedNameFString — hand-built asset ``qualifiedName`` grammar.

Atlan's ``qualifiedName`` is the identity primitive for every asset (dedup,
lineage, linking).  Building it by hand with an f-string — ``f"{connection_qualified_name}/{db}"``
— scatters the grammar (segments, order, separator, escaping) across every
connector.  A single grammar change then breaks each connector independently and
silently.  Construct qualifiedNames through the canonical pyatlan asset
``.creator()`` factories (which own the grammar) instead of string formatting.

Per-file.  Heuristic, WARN-tier: flag an f-string that both (a) interpolates a
``*qualified_name`` / ``*_qn`` value and (b) contains a ``/`` path separator —
i.e. is composing a slash-delimited qualifiedName.  This catches the whole chain
(``db_qn = f"{connection_qn}/{db}"`` → ``schema_qn = f"{db_qn}/{schema}"`` …).
"""

from __future__ import annotations

import ast
import re

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

# A referenced identifier that names a qualifiedName value.
_QN_NAME = re.compile(r"(?i)(qualified_name|_qn$|^qn$)")


def _references_qualified_name(joined: ast.JoinedStr) -> bool:
    for part in joined.values:
        if not isinstance(part, ast.FormattedValue):
            continue
        for sub in ast.walk(part.value):
            ident: str | None = None
            if isinstance(sub, ast.Name):
                ident = sub.id
            elif isinstance(sub, ast.Attribute):
                ident = sub.attr
            if ident and _QN_NAME.search(ident):
                return True
    return False


def _has_path_separator(joined: ast.JoinedStr) -> bool:
    return any(
        isinstance(part, ast.Constant)
        and isinstance(part.value, str)
        and "/" in part.value
        for part in joined.values
    )


def check_p028(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P028 for f-strings composing a slash-delimited qualifiedName."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        if _references_qualified_name(node) and _has_path_separator(node):
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P028",
                    node=node,
                    message=(
                        "qualifiedName built by hand with an f-string — Atlan's "
                        "qualifiedName grammar should not be duplicated across "
                        "connectors. Construct assets via the pyatlan asset "
                        ".creator() factories, which own the grammar."
                    ),
                    directives=directives,
                )
            )
    return findings
