"""P009 ManualStoreConstruction — direct object-store / cloud-client construction.

App code must obtain storage from ``get_infrastructure().storage`` (or
``App.upload()``/``download()``); the SDK wires the correct backing store from
deployment configuration.  Constructing a ``boto3`` client, an ``obstore``
store, or a store binding by hand bypasses that wiring.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_OBSTORE_MODULES: frozenset[str] = frozenset({"obstore", "obstore.store"})
_OBSTORE_STORE_TYPES: frozenset[str] = frozenset(
    {"S3Store", "GCSStore", "AzureStore", "HTTPStore", "MicrosoftAzureStore"}
)
_BINDING_FACTORIES: frozenset[str] = frozenset(
    {
        "create_store_from_binding",
        "create_store_from_binding_optional",
        "create_store_from_binding_with_put_attrs",
        "create_store_from_binding_optional_with_put_attrs",
    }
)


def _is_storage_module(module: str | None) -> bool:
    return module is not None and (
        module == "application_sdk.storage"
        or module.startswith("application_sdk.storage.")
    )


def _check_boto3(
    tree: ast.AST, filename: str, directives, findings: list[Finding]
) -> None:
    message = (
        "Direct 'boto3' import — use get_infrastructure().storage or App.upload() "
        "instead of constructing a cloud client directly."
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "boto3" or alias.name.startswith("boto3."):
                    findings.append(
                        make_finding(
                            filename=filename,
                            rule_id="P009",
                            node=node,
                            message=message,
                            directives=directives,
                        )
                    )
                    break
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if module == "boto3" or (
                module is not None and module.startswith("boto3.")
            ):
                findings.append(
                    make_finding(
                        filename=filename,
                        rule_id="P009",
                        node=node,
                        message=message,
                        directives=directives,
                    )
                )


def check_p009(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P009 for manual boto3 / obstore / store-binding construction."""
    findings: list[Finding] = []

    # Sub-check A: boto3 imports.
    _check_boto3(tree, filename, directives, findings)

    # Collect import provenance for sub-checks B and C.
    obstore_names: set[str] = set()
    binding_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module in _OBSTORE_MODULES:
            for alias in node.names:
                if alias.name in _OBSTORE_STORE_TYPES:
                    obstore_names.add(alias.asname or alias.name)
        if _is_storage_module(node.module):
            for alias in node.names:
                if alias.name in _BINDING_FACTORIES:
                    binding_names.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # Sub-check B: obstore store constructors actually imported from obstore.
        if isinstance(func, ast.Name) and func.id in obstore_names:
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P009",
                    node=node,
                    message=(
                        f"Direct obstore store construction ({func.id}(...)) — use "
                        "get_infrastructure().storage instead of constructing stores "
                        "ad-hoc. The SDK wires the correct store based on deployment "
                        "configuration."
                    ),
                    directives=directives,
                )
            )
            continue

        # Sub-check C: create_store_from_binding-family calls (name or attribute).
        binding_call = (isinstance(func, ast.Name) and func.id in binding_names) or (
            isinstance(func, ast.Attribute) and func.attr in _BINDING_FACTORIES
        )
        if binding_call:
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="P009",
                    node=node,
                    message=(
                        "Direct create_store_from_binding() call — use "
                        "get_infrastructure().storage (already initialised by the "
                        "SDK) instead of building a new store binding."
                    ),
                    directives=directives,
                )
            )

    return findings
