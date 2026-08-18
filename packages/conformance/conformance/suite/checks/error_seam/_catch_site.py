"""P043 NonPublicErrorControlFlow — ban control flow on SDK-internal error classes.

The import is the coupling; this is the defect.  When the SDK changes which
class a boundary surfaces, an ``except`` on a sibling class silently stops
matching and the guard becomes dead code — no error, no warning, just a handler
that never runs (CONNECT-970).

Five shapes make a class's identity load-bearing:

* ``except X`` and ``except (X, Y)``
* ``isinstance(e, X)`` and ``issubclass(t, X)``
* ``class Y(X)``

A bare annotation is not flagged: it does not change behaviour, and P045 already
covers the import that made it possible.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    collect_import_origins,
    make_finding,
)
from conformance.suite.schema.findings import Finding

from ._public_error_surface import PUBLIC_ERROR_MODULE, covered_error_name, remediation

_NARROWING_BUILTINS = frozenset({"isinstance", "issubclass"})


def _named_operands(node: ast.expr | None) -> list[ast.Name]:
    """Flatten a class reference that may be a bare name or a tuple of names."""
    if isinstance(node, ast.Name):
        return [node]
    if isinstance(node, ast.Tuple):
        return [elt for elt in node.elts if isinstance(elt, ast.Name)]
    return []


def check_p043(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit one P043 finding per reference that makes an internal class load-bearing."""
    origins = collect_import_origins(tree)
    findings: list[Finding] = []

    def record(name_node: ast.Name, shape: str) -> None:
        name = covered_error_name(origins.get(name_node.id))
        if name is None:
            return
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P043",
                node=name_node,
                message=(
                    f"{shape} depends on '{name}', an SDK error class that "
                    f"'{PUBLIC_ERROR_MODULE}' does not export. The SDK can change "
                    f"which class a boundary surfaces in a minor release, and a "
                    f"sibling class silently stops matching — the guard becomes "
                    f"dead code with no error. {remediation(name)} Suppress with "
                    f"'# conformance: ignore[P043] <reason>'."
                ),
                directives=directives,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for operand in _named_operands(node.type):
                record(operand, "This 'except' clause")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _NARROWING_BUILTINS:
                if len(node.args) >= 2:
                    for operand in _named_operands(node.args[1]):
                        record(operand, f"This '{node.func.id}()' check")
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name):
                    record(base, "This class's base")

    return findings
