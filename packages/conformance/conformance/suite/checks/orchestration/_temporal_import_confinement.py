"""P006 TemporalImportOutsideAdapter — keep Temporal contained in the SDK.

For the SDK to stay the single seam, ``temporalio`` must be imported only inside
the orchestration adapter (``application_sdk/execution/_temporal/``) — plus the
curated primitive re-export site ``application_sdk/app/__init__.py``, which is the
seam apps consume.  A ``temporalio`` import anywhere else bleeds the engine across
the SDK and is flagged so it can be relocated behind the adapter (BLDX-1417).
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._temporal_common import (
    in_adapter,
    is_primitive_reexport_file,
    is_test_file,
    iter_temporal_imports,
)


def check_p006(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit a P006 finding for ``temporalio`` imports outside the blessed boundary."""
    if (
        in_adapter(filename)
        or is_primitive_reexport_file(filename)
        or is_test_file(filename)
    ):
        return []
    findings: list[Finding] = []
    for node in iter_temporal_imports(tree):
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P006",
                node=node,
                message=(
                    "'temporalio' imported outside the orchestration adapter. The "
                    "SDK keeps Temporal contained behind "
                    "'application_sdk/execution/_temporal/' so it stays the single "
                    "seam — relocate this usage into the adapter (or behind it). "
                    "Suppress with '# conformance: ignore[P006] <reason>'."
                ),
                directives=directives,
            )
        )
    return findings
