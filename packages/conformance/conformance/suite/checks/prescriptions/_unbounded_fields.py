"""P001 UnboundedContractFields — flag ``allow_unbounded_fields=True`` contracts.

An ``Input``/``Output`` contract subclass declared with the
``allow_unbounded_fields=True`` class keyword opts out of payload-safety
enforcement; the opt-out must be an inline, justified suppression at the
declaration site.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding


class UnboundedContractFieldsChecker(ast.NodeVisitor):
    """Walk a module AST and emit P001 findings."""

    _CONTRACT_BASES = frozenset(
        {
            "Input",
            "Output",
            "ExtractionInput",
            "ExtractionTaskInput",
        }
    )

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._findings: list[Finding] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # ``allow_unbounded_fields`` is an SDK contract option.  Other classes
        # may use the same keyword for their own purposes and are outside P001.
        if not any(
            isinstance(base, ast.Name) and base.id in self._CONTRACT_BASES
            for base in node.bases
        ):
            self.generic_visit(node)
            return
        for kw in node.keywords:
            if kw.arg != "allow_unbounded_fields":
                continue
            # The opt-out is active for ANY truthy value: Input/Output's
            # __init_subclass__ does ``if allow_unbounded_fields:``.  So
            # ``=True``, ``=1`` and dynamic values (``=FLAG``, ``=(expr)``) all
            # opt out.  Only an explicit literal-falsy value (False/None/0/"")
            # is a genuine opt-back-in and must NOT be flagged.
            if isinstance(kw.value, ast.Constant) and not kw.value.value:
                break
            self._findings.append(
                make_finding(
                    filename=self._filename,
                    rule_id="P001",
                    node=node,
                    message=(
                        f"Contract '{node.name}' opts out of payload-safety "
                        "enforcement via allow_unbounded_fields — arbitrary untyped "
                        "fields may cross task boundaries. This must be exceptional: "
                        "justify it with an inline '# conformance: ignore[P001] "
                        "<reason>' directive at the declaration site (and prefer a "
                        "non-dynamic value so the opt-out is statically auditable)."
                    ),
                    directives=self._directives,
                )
            )
            break
        self.generic_visit(node)
