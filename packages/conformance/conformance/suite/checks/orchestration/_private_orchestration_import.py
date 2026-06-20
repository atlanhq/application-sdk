"""P005 PrivateOrchestrationInternalImport — ban reaching into SDK internals.

The SDK's public orchestration seam is ``application_sdk.app`` and
``application_sdk.execution``.  Importing past it into private modules (anything
with a ``_``-prefixed segment, e.g. ``application_sdk.execution._temporal.*``) or
importing a private (``_``-prefixed) name from a public SDK module couples the
app to internals that can change without notice (BLDX-1417).
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._temporal_common import private_sdk_import_name, private_sdk_target


def check_p005(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit a P005 finding for every import that reaches SDK-private internals."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        target: str | None = None
        if isinstance(node, ast.ImportFrom):
            target = private_sdk_target(node)
        elif isinstance(node, ast.Import):
            target = next(
                (a.name for a in node.names if private_sdk_import_name(a.name)),
                None,
            )
        if target is None:
            continue
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P005",
                node=node,
                message=(
                    f"Imports SDK-private orchestration internals ('{target}') — "
                    "this reaches past the public seam. Use the public re-exports "
                    "from 'application_sdk.execution' / 'application_sdk.app' "
                    "instead. If no public equivalent exists, raise it with the SDK "
                    "team; suppress with '# conformance: ignore[P005] <reason>'."
                ),
                directives=directives,
            )
        )
    return findings
