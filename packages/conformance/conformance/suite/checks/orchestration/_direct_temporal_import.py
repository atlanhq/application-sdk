"""P004 DirectTemporalImport — ban direct ``temporalio`` imports in app code.

Apps must interact with the orchestration layer only through the SDK seam, never
the underlying Temporal engine.  Any ``import temporalio`` / ``from temporalio …``
in a consumer app is a violation (BLDX-1417).
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._temporal_common import iter_temporal_imports, temporal_import_hint


def check_p004(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit a P004 finding for every direct ``temporalio`` import."""
    findings: list[Finding] = []
    for node in iter_temporal_imports(tree):
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P004",
                node=node,
                message=(
                    "Direct 'temporalio' import — apps must reach the orchestration "
                    "layer only through the SDK seam, not the engine directly. "
                    f"{temporal_import_hint(node)} If this is genuinely unavoidable, "
                    "justify it with '# conformance: ignore[P004] <reason>'."
                ),
                directives=directives,
            )
        )
    return findings
