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
    counts. A same-named class defined locally, or imported from any other
    module, is not an SDK base.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level > 0:
            continue
        module = node.module or ""
        if module == _SDK_MODULE or module.startswith(_SDK_MODULE + "."):
            bound.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in SDK_APP_BASE_NAMES
            )
    return frozenset(bound)
