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
    level.
    """
    bound: set[str] = set()
    sdk_star = foreign_star = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        is_sdk = node.level == 0 and (
            module == _SDK_MODULE or module.startswith(_SDK_MODULE + ".")
        )
        if any(alias.name == "*" for alias in node.names):
            sdk_star = sdk_star or is_sdk
            foreign_star = foreign_star or not is_sdk
        elif is_sdk:
            bound.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in SDK_APP_BASE_NAMES
            )
    if sdk_star and not foreign_star:
        bound |= SDK_APP_BASE_NAMES - non_sdk_import_bindings(tree)
    return frozenset(bound - _module_level_definitions(tree))


def non_sdk_import_bindings(tree: ast.AST) -> frozenset[str]:
    """Local names *tree* binds by a ``from … import`` of any non-SDK module.

    A relative import counts as non-SDK: the SDK is never imported relatively
    from a consumer repo.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level == 0 and (
            module == _SDK_MODULE or module.startswith(_SDK_MODULE + ".")
        ):
            continue
        bound.update(alias.asname or alias.name for alias in node.names)
    return frozenset(bound - {"*"})


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
