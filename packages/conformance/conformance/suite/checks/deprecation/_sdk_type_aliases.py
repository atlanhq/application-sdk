"""The SDK's public type aliases, baked into the wheel for B005.

B005 expands type aliases before it compares a live contract field against the
ledger, so ``field: Filter`` with ``Filter = dict[str, str]`` is judged as the
``dict`` it stands for.  Aliases an app defines in its own module are read off
the app's own AST.  Aliases it *imports from the SDK* — ``FilterMap``, which the
P001 prescription tells apps to use — cannot be: the suite runs inside consumer
app repos from an isolated environment where the SDK source is not installed, so
an imported alias used to stay a bare name and a correct retype off ``Any`` read
as a type change.

So the SDK's aliases ship as committed data, the same mechanism as the
deprecation manifest, the public-error allowlist and the toolkit baseline:

    uv run atlan-application-sdk-conformance gen-sdk-type-aliases

reads ``application_sdk/`` at SDK-dev time; ``--check`` and a drift test keep
the committed JSON equal to the source.

Resolution is limited to **directly bound names** — ``from application_sdk.X
import Y [as Z]`` at module level — the same scope as the error-seam rules.  The
table only ever *expands* a name B005 would otherwise leave opaque, so a missing
or stale file makes B005 fire as it did before, never go quiet.
"""

from __future__ import annotations

import ast
import copy
import importlib.resources as _ir
import json
from functools import lru_cache
from pathlib import Path

SDK_PACKAGE = "application_sdk"

_DATA_RELPATH: tuple[str, ...] = ("data", "sdk_type_aliases.json")

_REEXPORT_PASSES = 8


def _data_path() -> Path:
    return Path(str(_ir.files("conformance"))).joinpath(*_DATA_RELPATH)


DATA_PATH = _data_path()


def _module_name(sdk_root: Path, py: Path) -> str:
    parts = list(py.relative_to(sdk_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _absolute_source(module: str, is_package: bool, stmt: ast.ImportFrom) -> str | None:
    if stmt.level == 0:
        return stmt.module
    package = module if is_package else module.rpartition(".")[0]
    for _ in range(stmt.level - 1):
        package = package.rpartition(".")[0]
    if not package:
        return None
    return f"{package}.{stmt.module}" if stmt.module else package


def build_sdk_type_aliases(sdk_root: Path) -> dict[str, str]:
    """Map ``module.Name`` to the source of every public SDK type alias.

    Each alias is expanded through its own module's aliases first, so the stored
    expression never names another SDK alias.  Public re-exports
    (``from .sql_metadata import FilterMap`` in a package ``__init__``) are added
    under the re-exporting module's path too, since that is the path apps import.
    """
    from conformance.suite.checks.deprecation._contract_compat import (
        _AliasBudgetExceeded,
        _AliasExpander,
        collect_type_aliases,
    )

    package_dir = sdk_root / SDK_PACKAGE
    modules: dict[str, tuple[ast.Module, bool]] = {}
    for py in sorted(package_dir.rglob("*.py")):
        module = _module_name(sdk_root, py)
        if any(part.startswith("_") for part in module.split(".")[1:]):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        modules[module] = (tree, py.name == "__init__.py")

    table: dict[str, str] = {}
    for module, (tree, _) in modules.items():
        local = collect_type_aliases(tree)
        for name, expr in local.items():
            if name.startswith("_"):
                continue
            try:
                expanded = _AliasExpander(local, frozenset({name})).visit(
                    copy.deepcopy(expr)
                )
            except _AliasBudgetExceeded:
                expanded = expr
            table[f"{module}.{name}"] = ast.unparse(expanded)

    for _ in range(_REEXPORT_PASSES):
        added = False
        for module, (tree, is_package) in modules.items():
            for stmt in tree.body:
                if not isinstance(stmt, ast.ImportFrom):
                    continue
                source = _absolute_source(module, is_package, stmt)
                if not source or not source.startswith(SDK_PACKAGE):
                    continue
                for alias in stmt.names:
                    bound = alias.asname or alias.name
                    if bound.startswith("_"):
                        continue
                    origin = table.get(f"{source}.{alias.name}")
                    target = f"{module}.{bound}"
                    if origin is not None and target not in table:
                        table[target] = origin
                        added = True
        if not added:
            break
    return dict(sorted(table.items()))


def serialize(table: dict[str, str]) -> str:
    """Deterministic JSON so ``--check`` is a stable staleness gate."""
    return json.dumps({"type_aliases": table}, indent=2, sort_keys=True) + "\n"


@lru_cache(maxsize=1)
def load_sdk_type_aliases() -> dict[str, ast.expr]:
    """The committed table, parsed; empty when absent or unparseable."""
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    raw = data.get("type_aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, ast.expr] = {}
    for qualname, source in raw.items():
        if not isinstance(qualname, str) or not isinstance(source, str):
            continue
        try:
            parsed[qualname] = ast.parse(source, mode="eval").body
        except SyntaxError:
            continue
    return parsed


def collect_sdk_imported_aliases(tree: ast.AST) -> dict[str, ast.expr]:
    """SDK type aliases this module binds by ``from application_sdk… import``."""
    if not isinstance(tree, ast.Module):
        return {}
    table = load_sdk_type_aliases()
    if not table:
        return {}
    bound: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.ImportFrom) or stmt.level != 0 or not stmt.module:
            continue
        if stmt.module != SDK_PACKAGE and not stmt.module.startswith(f"{SDK_PACKAGE}."):
            continue
        for alias in stmt.names:
            expr = table.get(f"{stmt.module}.{alias.name}")
            if expr is not None:
                bound[alias.asname or alias.name] = expr
    return bound
