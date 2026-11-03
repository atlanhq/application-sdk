"""P004 DirectTemporalImport — ban direct ``temporalio`` imports in app code.

Apps must interact with the orchestration layer only through the SDK seam, never
the underlying Temporal engine.  Any ``import temporalio`` / ``from temporalio …``
in a consumer app is a violation (BLDX-1417).

A module-level, one-line import of a single temporalio failure class that
``application_sdk.execution`` re-exports under a ``Temporal*`` alias is marked
as a safe swap: the alias is the same class object, and the swap keeps the
name the module binds, so nothing else in the file changes.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks._ast_common._safe_swap import alias_swap, top_level_ids
from conformance.suite.schema.findings import Finding

from ._temporal_common import iter_temporal_imports, temporal_import_hint


def check_p004(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
    *,
    lines: Sequence[str],
) -> list[Finding]:
    """Emit a P004 finding for every direct ``temporalio`` import."""
    findings: list[Finding] = []
    top_level = top_level_ids(tree)
    for node in iter_temporal_imports(tree):
        swap = alias_swap(node, top_level, lines)
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
                properties=swap.to_properties() if swap else None,
            )
        )
    return findings
