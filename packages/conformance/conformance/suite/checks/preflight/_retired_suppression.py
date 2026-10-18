"""F020: a suppression directive that cites a retired preflight id.

The directive parser matches ids as plain strings, so ``ignore[P034]`` keeps
parsing after the rename, suppresses nothing, and the renamed rule fires with
no hint why. This pass names the replacement so the fix is one edit. F017 and
F018 were retired with no replacement, so a directive citing them is simply
dead and the fix is to delete it.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry

RETIRED_IDS: dict[str, str | None] = {
    "P032": "F001",
    "P033": "F002",
    "P034": "F003",
    "P035": "F004",
    "P047": "F005",
    "F017": None,
    "F018": None,
}


def _message(old: str) -> str:
    new = RETIRED_IDS[old]
    if new is None:
        return (
            f"Suppression cites retired id {old}; the rule was retired with no "
            "replacement and this directive no longer suppresses anything. Delete it."
        )
    return (
        f"Suppression cites retired id {old}; the rule is now {new} and this "
        f"directive no longer suppresses anything. Cite {new} or delete the directive."
    )


def scan(reg: Registry) -> list[Finding]:
    findings: list[Finding] = []
    for src in reg.sources:
        for lineno, directive in sorted(src.directives.items()):
            for old in sorted(RETIRED_IDS.keys() & (directive.rule_ids or frozenset())):
                findings.append(
                    make_finding(
                        filename=src.rel,
                        rule_id="F020",
                        node=ast.Pass(lineno=lineno, col_offset=0),
                        message=_message(old),
                        directives={},
                    )
                )
    return findings
