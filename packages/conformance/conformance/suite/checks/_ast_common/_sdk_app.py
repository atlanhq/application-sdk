"""SDK ``App``-family bases: ``App`` plus ``application_sdk.templates.__all__``."""

from __future__ import annotations

import ast

SDK_APP_BASE_NAMES: frozenset[str] = frozenset(
    {
        "App",
        "SqlApp",
        "BaseMetadataExtractor",
        "IncrementalSqlMetadataExtractor",
        "SqlMetadataExtractor",
        "SqlQueryExtractor",
    }
)

_SDK_MODULE = "application_sdk"


def sdk_app_base_bindings(tree: ast.AST) -> frozenset[str]:
    """Local names *tree* binds to an SDK ``App``-family base.

    Only an absolute ``from application_sdk[.<sub>] import <Base> [as <alias>]``
    counts, or ``from application_sdk[.<sub>] import *`` for a name nothing
    else in the module binds, when no non-SDK star import could bind it too.
    A same-named class defined locally, or imported from any other module, is
    not an SDK base, and neither is an SDK name the module rebinds at top
    level. Imports are read in source order, so a later ``from <other> import``
    of the same name shadows an earlier SDK import (and vice versa).
    """
    bound: set[str] = set()
    sdk_star = foreign_star = False
    for node in _import_froms_in_source_order(tree):
        is_sdk = _is_sdk_import(node)
        if any(alias.name == "*" for alias in node.names):
            sdk_star = sdk_star or is_sdk
            foreign_star = foreign_star or not is_sdk
            continue
        for alias in node.names:
            local = alias.asname or alias.name
            if is_sdk and alias.name in SDK_APP_BASE_NAMES:
                bound.add(local)
            else:
                bound.discard(local)
    if sdk_star and not foreign_star:
        bound |= SDK_APP_BASE_NAMES - non_sdk_import_bindings(tree)
    return frozenset(bound - _module_level_definitions(tree))


def non_sdk_import_bindings(tree: ast.AST) -> frozenset[str]:
    """Local names *tree* binds by a ``from … import`` of any non-SDK module.

    A relative import counts as non-SDK: the SDK is never imported relatively
    from a consumer repo.
    """
    return frozenset(non_sdk_import_roots(tree))


def non_sdk_import_roots(tree: ast.AST) -> dict[str, str | None]:
    """Each name *tree* binds by a non-SDK ``from … import``, mapped to its top-level package.

    ``from vendor.models import Input`` maps ``Input`` to ``"vendor"``; a
    relative import maps to ``None`` (it is in-repo by construction). Callers
    compare the root against the scanned repo's packages to tell an in-repo
    import from a third-party one.
    """
    roots: dict[str, str | None] = {}
    for node in _import_froms_in_source_order(tree):
        if _is_sdk_import(node):
            continue
        root = (node.module or "").split(".")[0] if node.level == 0 else None
        for alias in node.names:
            if alias.name != "*":
                roots[alias.asname or alias.name] = root
    return roots


def module_shadowing_bindings(tree: ast.AST) -> frozenset[str]:
    """Local names *tree* binds other than to an SDK class.

    A top-level definition or assignment, or a non-SDK ``from … import``. A
    base spelled with one of these names refers to that binding, not to the
    SDK class of the same name.
    """
    return non_sdk_import_bindings(tree) | frozenset(_module_level_definitions(tree))


def _is_sdk_import(node: ast.ImportFrom) -> bool:
    module = node.module or ""
    return node.level == 0 and (
        module == _SDK_MODULE or module.startswith(_SDK_MODULE + ".")
    )


def _import_froms_in_source_order(tree: ast.AST) -> list[ast.ImportFrom]:
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    return sorted(nodes, key=lambda n: (n.lineno, n.col_offset))


def _module_level_definitions(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names
