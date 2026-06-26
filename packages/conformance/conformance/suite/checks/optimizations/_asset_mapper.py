"""O002 LegacyAssetSerialization + O003 UntypedAssetMapperReturn.

Asset-mapper hygiene recommendations (BLDX-1492).  Both are gated on the module
importing pyatlan asset models, so they never fire on non-connector code:

* **O002** — a ``.dict()`` call in such a module; the v3 pipeline serialises
  assets with ``asset.to_nested_bytes()``, not the pydantic ``.dict()`` form.
* **O003** — a function that constructs a pyatlan asset and returns *that asset*
  but declares no return annotation; the asset-mapper pattern is typed end-to-end.
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


def _iter_own_scope(func: ast.FunctionDef | ast.AsyncFunctionDef):
    """Yield descendants that execute in *func*'s own scope.

    Unlike :func:`ast.walk`, this does **not** descend into nested ``def`` /
    ``async def`` / ``lambda`` bodies, so a ``return`` or asset construction that
    belongs to an inner closure is not mis-attributed to the outer function.
    """
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    stack = [n for n in func.body if not isinstance(n, nested)]
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, nested):
                stack.append(child)


def _is_asset_call(node: ast.expr | None, asset_names: frozenset[str]) -> bool:
    """True if *node* is a call to an imported asset class, e.g. ``Table(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in asset_names
    )


def _returns_constructed_asset(
    func: ast.FunctionDef | ast.AsyncFunctionDef, asset_names: frozenset[str]
) -> bool:
    """True if *func* returns an asset it constructs.

    Catches the two common mapper shapes — ``return Table(...)`` and
    ``asset = Table(...); ...; return asset`` — and **only** those: a function
    that builds an asset as a side effect and returns something else (e.g.
    ``return record.id``) is not flagged, so the rule's ``-> <Asset>`` advice
    always matches the function's actual return.  Scoped to *func*'s own body
    (nested closures excluded).
    """
    # Pass 1 — local names bound to an asset constructor call.
    asset_bound: set[str] = set()
    for sub in _iter_own_scope(func):
        if isinstance(sub, ast.Assign) and _is_asset_call(sub.value, asset_names):
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    asset_bound.add(target.id)
        elif isinstance(sub, ast.AnnAssign) and _is_asset_call(sub.value, asset_names):
            if isinstance(sub.target, ast.Name):
                asset_bound.add(sub.target.id)

    # Pass 2 — a return of the asset itself (direct construction or bound name).
    for sub in _iter_own_scope(func):
        if isinstance(sub, ast.Return):
            value = sub.value
            if _is_asset_call(value, asset_names):
                return True
            if isinstance(value, ast.Name) and value.id in asset_bound:
                return True
    return False


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
            and _returns_constructed_asset(node, asset_names)
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
