"""P045 PrivateErrorClassImport — ban importing SDK-internal error classes.

An error class outside ``application_sdk.errors.__all__`` is not a contract.
Importing one couples the app to a module layout the SDK can change in a minor
release (CONNECT-970).
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._public_error_surface import (
    COVERED_MODULE_PREFIX,
    PUBLIC_ERROR_MODULE,
    remediation,
)


def check_p044(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit one P045 finding per import statement that binds an internal error class."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        # level > 0 is a relative import — never the application_sdk distribution.
        if node.level != 0 or not node.module:
            continue
        if not (node.module + ".").startswith(COVERED_MODULE_PREFIX):
            continue
        names = [a.name for a in node.names if a.name.endswith("Error")]
        if not names:
            continue
        listed = ", ".join(f"'{n}'" for n in names)
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P045",
                node=node,
                message=(
                    f"Imports SDK-internal error class(es) {listed} from "
                    f"'{node.module}'. Only '{PUBLIC_ERROR_MODULE}' is the public "
                    f"error contract; classes elsewhere can move or change which "
                    f"boundary surfaces them in a minor release, with no "
                    f"deprecation cycle. {remediation(names[0])} Suppress with "
                    f"'# conformance: ignore[P045] <reason>'."
                ),
                directives=directives,
            )
        )
    return findings
