"""P010 FileReferenceManagedField — ``FileReference(...)`` set with SDK-managed fields.

``storage_path``, ``is_durable`` and ``file_count`` are populated by the SDK's
activity interceptor as a ref is durably stored.  Setting them by hand at
construction time fabricates durability state.  Construct refs with
``FileReference.from_local(path, tier=...)`` instead.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_MANAGED_FIELDS: tuple[str, ...] = ("storage_path", "is_durable", "file_count")


def _is_file_reference_call(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "FileReference") or (
        isinstance(func, ast.Attribute) and func.attr == "FileReference"
    )


def check_p010(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P010 for ``FileReference(...)`` constructed with SDK-managed fields."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_file_reference_call(node.func):
            continue
        offending = [kw.arg for kw in node.keywords if kw.arg in _MANAGED_FIELDS]
        if not offending:
            continue
        kwarg_names = ", ".join(offending)
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P010",
                node=node,
                message=(
                    f"FileReference(...) sets SDK-managed field(s) {kwarg_names} — "
                    "the SDK's activity interceptor manages these. Use "
                    "FileReference.from_local(path, tier=...) to construct refs."
                ),
                directives=directives,
            )
        )
    return findings
