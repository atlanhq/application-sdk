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

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.extensions import SafeSwap, SafeSwapRequires
from conformance.suite.schema.findings import Finding

from ._temporal_common import iter_temporal_imports, temporal_import_hint

PUBLIC_ALIAS_MODULE = "application_sdk.execution"

PUBLIC_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("temporalio.exceptions", "ActivityError"): ("TemporalActivityError", "3.21.0"),
    ("temporalio.exceptions", "ApplicationError"): (
        "TemporalApplicationError",
        "3.50.0",
    ),
    ("temporalio.exceptions", "CancelledError"): ("TemporalCancelledError", "3.21.0"),
    ("temporalio.exceptions", "ChildWorkflowError"): (
        "TemporalChildWorkflowError",
        "3.21.0",
    ),
    ("temporalio.exceptions", "TerminatedError"): ("TemporalTerminatedError", "3.21.0"),
    ("temporalio.exceptions", "TimeoutError"): ("TemporalTimeoutError", "3.21.0"),
    ("temporalio.client", "WorkflowFailureError"): (
        "TemporalWorkflowFailureError",
        "3.21.0",
    ),
}


def _alias_swap(node: ast.stmt, top_level: set[int]) -> SafeSwap | None:
    if (
        id(node) not in top_level
        or not isinstance(node, ast.ImportFrom)
        or len(node.names) != 1
        or node.lineno != node.end_lineno
    ):
        return None
    module = node.module or ""
    alias = node.names[0]
    entry = PUBLIC_ALIASES.get((module, alias.name))
    if entry is None:
        return None
    public, since = entry
    bound = "" if alias.asname else f" as {alias.name}"
    return SafeSwap(
        id="public-alias",
        find=f"{module} import {alias.name}",
        replace=f"{PUBLIC_ALIAS_MODULE} import {public}{bound}",
        requires=SafeSwapRequires(package="atlan-application-sdk", min=since),
    )


def check_p004(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit a P004 finding for every direct ``temporalio`` import."""
    findings: list[Finding] = []
    top_level = {id(n) for n in getattr(tree, "body", [])}
    for node in iter_temporal_imports(tree):
        swap = _alias_swap(node, top_level)
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
