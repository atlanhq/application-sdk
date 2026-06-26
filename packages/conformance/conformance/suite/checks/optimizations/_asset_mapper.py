"""O002 LegacyAssetSerialization + O003 UntypedAssetMapperReturn.

Asset-mapper hygiene recommendations (BLDX-1492).  Both are gated on the module
importing pyatlan asset models, so they never fire on non-connector code:

* **O002** — a ``.dict()`` call in such a module; the v3 pipeline serialises
  assets with ``asset.to_nested_bytes()``, not the pydantic ``.dict()`` form.
* **O003** — a function that constructs a pyatlan asset and returns a value but
  declares no return annotation; the asset-mapper pattern is typed end-to-end.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_ASSET_MODULES = ("pyatlan_v9.model.assets", "pyatlan.model.assets")

_O002_MESSAGE = (
    "Asset serialised with .dict() — use the v9 asset.to_nested_bytes() API "
    "instead (emits the nested-entity wire shape the asset-mapper pipeline "
    "expects). If this .dict() is on a non-asset model, suppress with "
    "# conformance: ignore[O002] <reason>."
)
_O003_MESSAGE = (
    "Function builds a pyatlan asset but has no return annotation — annotate it "
    "with the asset type it returns (e.g. -> Table) so the mapper is typed "
    "end-to-end, like the reference asset-mapper apps."
)


def _module_matches(module: str | None) -> bool:
    return module is not None and (
        module in _ASSET_MODULES
        or any(module.startswith(f"{m}.") for m in _ASSET_MODULES)
    )


def _collect_asset_imports(tree: ast.AST) -> tuple[bool, frozenset[str]]:
    """Return ``(file_imports_assets, asset_class_names)``.

    ``asset_class_names`` are local names bound to classes imported via
    ``from pyatlan[_v9].model.assets import X [as y]``.  ``file_imports_assets``
    also covers the ``import pyatlan[_v9].model.assets`` module form.
    """
    names: set[str] = set()
    imports_assets = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if _module_matches(node.module):
                imports_assets = True
                for alias in node.names:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _module_matches(alias.name):
                    imports_assets = True
    return imports_assets, frozenset(names)


def check_o002(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit O002 for ``.dict()`` calls in a module that imports asset models."""
    imports_assets, _ = _collect_asset_imports(tree)
    if not imports_assets:
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dict"
        ):
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="O002",
                    node=node,
                    message=_O002_MESSAGE,
                    directives=directives,
                )
            )
    return findings


def _constructs_asset_and_returns(
    func: ast.FunctionDef | ast.AsyncFunctionDef, asset_names: frozenset[str]
) -> bool:
    """True if *func* instantiates an imported asset class and returns a value."""
    constructs = False
    returns_value = False
    for sub in ast.walk(func):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id in asset_names
        ):
            constructs = True
        elif isinstance(sub, ast.Return) and sub.value is not None:
            returns_value = True
    return constructs and returns_value


def check_o003(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit O003 for asset-building functions that lack a return annotation."""
    _, asset_names = _collect_asset_imports(tree)
    if not asset_names:
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.returns is None
            and _constructs_asset_and_returns(node, asset_names)
        ):
            findings.append(
                make_finding(
                    filename=filename,
                    rule_id="O003",
                    node=node,
                    message=_O003_MESSAGE,
                    directives=directives,
                )
            )
    return findings
