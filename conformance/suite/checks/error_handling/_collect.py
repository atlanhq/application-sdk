"""Import-collection helpers for E013 IOError disambiguation and alias detection."""

from __future__ import annotations

import ast

from ._constants import _ATLAN_LEGACY_MODULE, LEGACY_ATLAN_ERRORS


def _collect_imports(tree: ast.Module) -> dict[str, str]:
    """Return ``{imported_name: source_module}`` for top-level imports."""
    imports: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name.split(".")[0]
                imports[name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname if alias.asname else alias.name
                imports[name] = module
    return imports


def _collect_legacy_aliases(tree: ast.Module) -> frozenset[str]:
    """Return names bound to LEGACY_ATLAN_ERRORS via aliased imports from the legacy module."""
    aliases: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if (node.module or "") != _ATLAN_LEGACY_MODULE:
            continue
        for alias in node.names:
            if alias.name in LEGACY_ATLAN_ERRORS and alias.asname:
                aliases.add(alias.asname)
    return frozenset(aliases)
