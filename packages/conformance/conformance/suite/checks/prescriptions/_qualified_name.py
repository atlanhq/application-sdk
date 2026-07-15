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

An asset ``qualifiedName`` is always *rooted* at its parent qn: the interpolated
qn value is the leading segment (``f"{connection_qn}/collections/{id}"``).  An
object-store **key** merely *embeds* a qn after a literal namespace prefix
(``f"persistent-artifacts/apps/…/{connection_qn}/publish-state"``,
``f"argo-artifacts/{connection_qn}/current-state"``) — that is a storage path,
not an asset identity, and pyatlan ``.creator()`` has nothing to say about it.
So when the qn reference is preceded by a ``/``-bearing literal segment we treat
the f-string as an object-store key and do **not** flag it — this removes the
false positive without weakening detection of genuinely hand-rooted qualifiedNames.
"""

from __future__ import annotations

import ast
import re

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

# A referenced identifier that names a qualifiedName value.
_QN_NAME = re.compile(r"(?i)(qualified_name|_qn$|^qn$)")


def _formatted_value_is_qn(part: ast.FormattedValue) -> bool:
    """True when the interpolated expression references a qualifiedName value."""
    for sub in ast.walk(part.value):
        ident: str | None = None
        if isinstance(sub, ast.Name):
            ident = sub.id
        elif isinstance(sub, ast.Attribute):
            ident = sub.attr
        if ident and _QN_NAME.search(ident):
            return True
    return False


def _references_qualified_name(joined: ast.JoinedStr) -> bool:
    return any(
        isinstance(part, ast.FormattedValue) and _formatted_value_is_qn(part)
        for part in joined.values
    )


def _has_path_separator(joined: ast.JoinedStr) -> bool:
    return any(
        isinstance(part, ast.Constant)
        and isinstance(part.value, str)
        and "/" in part.value
        for part in joined.values
    )


def _is_object_store_key(joined: ast.JoinedStr) -> bool:
    """True when the qn reference is *embedded* after a literal path segment.

    An asset qualifiedName is rooted at its parent qn (``f"{parent_qn}/…"``);
    an object-store key prefixes the qn with a literal namespace segment
    (``f"persistent-artifacts/…/{connection_qn}/…"``).  We detect the latter by
    finding a ``/``-bearing string literal that precedes the first qn reference.
    """
    seen_path_literal = False
    for part in joined.values:
        if (
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and "/" in part.value
        ):
            seen_path_literal = True
        elif isinstance(part, ast.FormattedValue) and _formatted_value_is_qn(part):
            # First qn reference: it is a key iff a path literal came before it.
            return seen_path_literal
    return False


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
        if (
            _references_qualified_name(node)
            and _has_path_separator(node)
            and not _is_object_store_key(node)
        ):
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
