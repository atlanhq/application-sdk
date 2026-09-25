"""O002 LegacyAssetSerialization + O003 UntypedAssetMapperReturn.

Asset-mapper hygiene recommendations (BLDX-1492).  Both are gated on the module
importing pyatlan asset models, so they never fire on non-connector code:

* **O002** — a ``.dict()`` call in such a module; the v3 pipeline serialises
  assets through the SDK's ``entity_bytes`` seam, not the pydantic ``.dict()``
  form.
* **O003** — a function that constructs a pyatlan asset and returns *that asset*
  but declares no return annotation; the asset-mapper pattern is typed end-to-end.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_ASSET_MODULES = ("pyatlan_v9.model.assets", "pyatlan.model.assets")

#: Asset-model module per pyatlan generation.
_GENERATION_MODULES: dict[str, str] = {
    "legacy": "pyatlan.model.assets",
    "v9": "pyatlan_v9.model.assets",
}

_O002_MESSAGE = (
    "Asset serialised with .dict() — serialize through "
    "application_sdk.common.asset_serialization.entity_bytes instead (emits the "
    "nested-entity wire shape the asset-mapper pipeline expects). If this "
    ".dict() is on a non-asset model, suppress with "
    "# conformance: ignore[O002] <reason>."
)
# A legacy model handed to entity_bytes falls through to model_dump(), whose
# snake_case field names are not the Atlas wire shape — so for these the
# serialization switch cannot come first.
_O002_LEGACY_MESSAGE = (
    "Asset serialised with .dict() on a legacy pyatlan.model.assets model — "
    "migrate the asset to pyatlan_v9.model.assets first (O004), then serialize "
    "through application_sdk.common.asset_serialization.entity_bytes. Do not "
    "switch a legacy model to entity_bytes alone: it falls through to "
    "model_dump(), whose snake_case fields are not the Atlas wire shape. If this "
    ".dict() is on a non-asset model, suppress with "
    "# conformance: ignore[O002] <reason>."
)
_O002_MIXED_MESSAGE = (
    "Asset serialised with .dict() in a module importing both legacy "
    "pyatlan.model.assets and pyatlan_v9 models. If this receiver is a "
    "pyatlan_v9 asset, serialize through "
    "application_sdk.common.asset_serialization.entity_bytes. If it is a legacy "
    "model, migrate it to pyatlan_v9.model.assets first (O004): entity_bytes on "
    "a legacy model falls through to model_dump(), whose snake_case fields are "
    "not the Atlas wire shape. If this .dict() is on a non-asset model, suppress "
    "with # conformance: ignore[O002] <reason>."
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


def _asset_generations(tree: ast.AST) -> frozenset[str]:
    """Which pyatlan asset generations the module imports: ``legacy`` / ``v9``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules = [node.module]
        elif isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        else:
            continue
        for module in modules:
            if module is None:
                continue
            for generation, root in _GENERATION_MODULES.items():
                if module == root or module.startswith(f"{root}."):
                    found.add(generation)
    return frozenset(found)


def check_o002(
    tree: ast.AST, filename: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    """Emit O002 for ``.dict()`` calls in a module that imports asset models."""
    imports_assets, _ = _collect_asset_imports(tree)
    if not imports_assets:
        return []
    generations = _asset_generations(tree)
    if generations == {"legacy"}:
        message = _O002_LEGACY_MESSAGE
    elif "legacy" in generations:
        # Both generations in one module: the receiver's type is not known
        # statically, so the advice has to cover either.
        message = _O002_MIXED_MESSAGE
    else:
        message = _O002_MESSAGE
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
                    message=message,
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
