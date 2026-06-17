"""P002 CategoryFieldOverride — flag ``category`` ClassVar redeclarations.

A subclass of ``AppError`` (or any of its 14 categorical leaves) that
redeclares the ``category`` ``ClassVar`` in its own body drifts the canonical
``FailureCategory`` taxonomy.  Only the canonical SDK error classes themselves
are the defining sites for ``category``; all other subclasses must inherit it.

The checker builds a transitive closure of AppError-derived class names within
the file so second-generation overrides (a class whose parent is itself a
flagged override) are caught even when the intermediate class is not in the
canonical set.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.checks.error_handling._constants import LEAF_CLASSES
from conformance.suite.checks.error_handling._helpers import _get_name
from conformance.suite.schema.findings import Finding

# The canonical SDK error classes are the *sole defining sites* for ``category``.
# Any other class that subclasses one of them (directly or transitively) and
# assigns ``category`` in its own body is a taxonomy override.
_CANONICAL_ERROR_CLASSES: frozenset[str] = LEAF_CLASSES | {"AppError"}


def _collect_apperror_subclasses(
    tree: ast.Module, canonical: frozenset[str]
) -> frozenset[str]:
    """Collect all class names that transitively inherit from canonical error classes.

    Iterates to a fixed point so second-generation (and deeper) subclasses are
    caught even when an intermediate class is itself a flagged override that is
    not in *canonical*.
    """
    derived: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name in canonical or node.name in derived:
                continue
            for base in node.bases:
                name = _get_name(base)
                if name and (name in canonical or name in derived):
                    derived.add(node.name)
                    changed = True
                    break
    return frozenset(derived)


def _find_category_assignment(cls: ast.ClassDef) -> ast.stmt | None:
    """Return the first class-body statement assigning a value to ``category``.

    Matches annotated assignments (``category: ClassVar[...] = ...``) and plain
    assignments (``category = ...``).  Annotation-only forms with no value are
    not flagged — they refine the type without binding a new value.
    """
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign):
            if (
                isinstance(stmt.target, ast.Name)
                and stmt.target.id == "category"
                and stmt.value is not None
            ):
                return stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "category":
                    return stmt
    return None


class CategoryOverrideChecker(ast.NodeVisitor):
    """Walk a module AST and emit P002 findings."""

    def __init__(
        self,
        filename: str,
        directives: dict[int, _IgnoreDirective],
        apperror_subclasses: frozenset[str],
    ) -> None:
        self._filename = filename
        self._directives = directives
        self._apperror_subclasses = apperror_subclasses
        self._findings: list[Finding] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if (
            node.name not in _CANONICAL_ERROR_CLASSES
            and node.name in self._apperror_subclasses
        ):
            assign_node = _find_category_assignment(node)
            if assign_node is not None:
                self._findings.append(
                    make_finding(
                        filename=self._filename,
                        rule_id="P002",
                        node=assign_node,
                        message=(
                            f"Class '{node.name}' redeclares the `category` "
                            "ClassVar — drifts the canonical FailureCategory "
                            "taxonomy. Domain subclasses must inherit `category` "
                            "from their categorical-leaf parent and specialise via "
                            "`code` (and evidence fields) only. If the redeclaration "
                            "is genuinely necessary, justify it with an inline "
                            "'# conformance: ignore[P002] <reason>' directive at the "
                            "assignment site."
                        ),
                        directives=self._directives,
                    )
                )
        self.generic_visit(node)
