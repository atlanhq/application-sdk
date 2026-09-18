"""F020: a suppression directive that cites a preflight id retired by the F-series move.

The directive parser matches ids as plain strings, so ``ignore[P034]`` keeps
parsing after the rename, suppresses nothing, and the renamed rule fires with
no hint why. This pass names the replacement so the fix is one edit.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import make_finding
from conformance.suite.schema.findings import Finding

from ._common import Registry

RETIRED_IDS = {
    "P032": "F001",
    "P033": "F002",
    "P034": "F003",
    "P035": "F004",
    "P047": "F005",
}


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
                        message=(
                            f"Suppression cites retired id {old}; the rule is now "
                            f"{RETIRED_IDS[old]} and this directive no longer suppresses "
                            f"anything. Cite {RETIRED_IDS[old]} or delete the directive."
                        ),
                        directives={},
                    )
                )
    return findings
