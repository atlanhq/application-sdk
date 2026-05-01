"""Base infrastructure for v2→v3 AST codemods.

Provides BaseCodemod, AddImportTransformer, and CodemodPipeline.
Each codemod is a CSTTransformer subclass with a standard interface:

    codemod = MyCodemod(connector_type="sql_metadata")
    new_tree = codemod.transform(tree)
    print(codemod.changes)         # human-readable log
    print(codemod.imports_to_add)  # (module, name) pairs to import

CodemodPipeline chains codemods and applies import additions as a final pass.
"""

from __future__ import annotations

import libcst as cst


def _module_to_str(node: cst.Attribute | cst.Name | None) -> str:
    """Convert a dotted-name CST node to a string like ``a.b.c``."""
    if node is None:
        return ""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return f"{_module_to_str(node.value)}.{node.attr.value}"
    return ""


class BaseCodemod(cst.CSTTransformer):
    """Base class for all v3 migration codemods.

    Subclasses implement the CSTTransformer visitor methods and accumulate
    changes / imports_to_add as side-effects during traversal.
    """

    def __init__(self) -> None:
        super().__init__()
        self.changes: list[str] = []
        self.imports_to_add: list[tuple[str, str]] = []  # (module, name)
        self.imports_to_remove: list[tuple[str, str]] = []

    def transform(self, tree: cst.Module) -> cst.Module:
        """Apply this codemod to *tree* and return the modified tree."""
        return tree.visit(self)


class AddImportTransformer(cst.CSTTransformer):
    """Adds ``from module import name`` statements to a module.

    Applied as the final pass after all codemods have run.
    Deduplicates against existing imports so it is safe to call repeatedly.
    """

    def __init__(self, imports: list[tuple[str, str]]) -> None:
        super().__init__()
        self._imports = imports

    def leave_Module(
        self,
        original_node: cst.Module,
        updated_node: cst.Module,
    ) -> cst.Module:
        if not self._imports:
            return updated_node

        # Collect existing (module, name) pairs to avoid duplicates.
        existing: set[tuple[str, str]] = set()
        for stmt in updated_node.body:
            if isinstance(stmt, cst.SimpleStatementLine):
                for s in stmt.body:
                    if isinstance(s, cst.ImportFrom) and isinstance(
                        s.names, (list, tuple)
                    ):
                        mod = _module_to_str(s.module)
                        for alias in s.names:
                            if isinstance(alias, cst.ImportAlias) and isinstance(
                                alias.name, cst.Name
                            ):
                                existing.add((mod, alias.name.value))

        # Find insertion point: right after the last existing import statement.
        last_import_idx = -1
        for i, stmt in enumerate(updated_node.body):
            if isinstance(stmt, cst.SimpleStatementLine):
                for s in stmt.body:
                    if isinstance(s, (cst.Import, cst.ImportFrom)):
                        last_import_idx = i
                        break

        # Build deduplicated list of new import statements.
        new_stmts: list[cst.BaseStatement] = []
        for module, name in self._imports:
            if (module, name) not in existing:
                new_stmt = cst.parse_statement(f"from {module} import {name}\n")
                new_stmts.append(new_stmt)
                existing.add((module, name))

        if not new_stmts:
            return updated_node

        insert_idx = last_import_idx + 1
        new_body = (
            list(updated_node.body[:insert_idx])
            + new_stmts
            + list(updated_node.body[insert_idx:])
        )
        return updated_node.with_changes(body=new_body)


class CodemodPipeline:
    """Chains codemods: runs each in sequence, then applies import additions."""

    def __init__(self, codemods: list[BaseCodemod]) -> None:
        self._codemods = codemods

    def run(self, source: str) -> tuple[str, dict[str, list[str]]]:
        """Run all codemods on *source*.

        Returns ``(new_source, {codemod_class_name: [changes]})``.
        """
        tree = cst.parse_module(source)
        all_changes: dict[str, list[str]] = {}
        all_imports_to_add: list[tuple[str, str]] = []

        for codemod in self._codemods:
            tree = codemod.transform(tree)
            if codemod.changes:
                all_changes[type(codemod).__name__] = list(codemod.changes)
            all_imports_to_add.extend(codemod.imports_to_add)

        if all_imports_to_add:
            adder = AddImportTransformer(all_imports_to_add)
            tree = tree.visit(adder)

        return tree.code, all_changes
