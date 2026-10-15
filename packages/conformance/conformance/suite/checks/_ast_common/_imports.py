"""Import- and rebinding-resolution shared by every AST-based check series.

Answers "what fully-qualified symbol does this call resolve to?" — the
question every check that bans/requires a specific SDK call (P017/P018
worker/server bootstrap, T004 dev-entrypoint delegation, …) needs answered
the same way, regardless of whether the call site wrote a bare name
(``main()`` after ``from application_sdk.main import main``), a
single-level attribute (``sdkmain.main()`` after ``import
application_sdk.main as sdkmain``), or a bare dotted chain
(``application_sdk.main.main()`` after ``import application_sdk.main``).

It also answers the sibling question for *classes*: a check that resolves a
class by its bare name needs to see through a module-level rebinding
(``OpenAPIConnectorInput = AppInputContract``), because that is the shape a
pkl-generated contract takes in a connector app — imported under its generated
name and re-bound to a domain name. See :func:`collect_module_alias_targets`.
"""

from __future__ import annotations

import ast
from typing import TypeVar


def collect_import_origins(tree: ast.AST) -> dict[str, str]:
    """Map each bound name to its fully-qualified import origin (module.name).

    Walks the entire tree so lazy / in-function imports are caught.

    Examples::

        from application_sdk.execution import create_worker
        -> {"create_worker": "application_sdk.execution.create_worker"}

        from fastapi import FastAPI
        -> {"FastAPI": "fastapi.FastAPI"}

        import uvicorn
        -> {"uvicorn": "uvicorn"}

        from uvicorn import run
        -> {"run": "uvicorn.run"}
    """
    origins: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname if alias.asname else alias.name.split(".")[0]
                origins[bound] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:  # skip relative imports
                continue
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname if alias.asname else alias.name
                origins[bound] = f"{module}.{alias.name}" if module else alias.name
    return origins


def qualify_chained_attr_call(
    func: ast.Attribute, origins: dict[str, str]
) -> str | None:
    """Resolve a chained attribute call (X.Y.Z()) to its as-written dotted path.

    Handles the case where ``func.value`` is itself an ``ast.Attribute`` — i.e.
    bare dotted submodule calls like::

        import application_sdk.execution
        application_sdk.execution.create_worker(...)
        # → "application_sdk.execution.create_worker"

        import fastapi.applications
        fastapi.applications.FastAPI()
        # → "fastapi.applications.FastAPI"

    The single-level case (``func.value: ast.Name``) is handled separately by
    the callers using the origins dict directly.

    Returns the full as-written dotted path if the root name was imported, else
    ``None``.
    """
    attrs: list[str] = [func.attr]
    node: ast.expr = func.value
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in origins:
        return None
    # attrs collected outermost-first; reverse to reconstruct left-to-right.
    return node.id + "." + ".".join(reversed(attrs))


_RecordT = TypeVar("_RecordT")


def collect_module_alias_targets(
    tree: ast.AST, import_aliases: dict[str, str] | None = None
) -> dict[str, str]:
    """Return module-level ``Local = Target`` rebindings as ``{local: target_name}``.

    A contract generated from ``contract/app.pkl`` is imported under its
    generated name and re-bound to a domain name
    (``OpenAPIConnectorInput = AppInputContract``); the ``TypeAlias``-annotated
    form (``OpenAPIConnectorInput: TypeAlias = AppInputContract``) is the same
    shape.  A check that resolves classes by bare name sees neither: no
    ``ClassDef`` carries the local name, so the class reads as absent from a
    scan it is sitting in.

    Only a plain ``Name`` / ``Attribute`` right-hand side qualifies — ``X =
    list[Y]`` binds a different type, not another name for ``Y``, and treating
    it as one would credit ``X`` with ``Y``'s fields.  *import_aliases* (from
    ``collect_import_aliases``) de-aliases the right-hand side so a renamed
    import (``from … import AppInputContract as _Base``) still lands on the
    original class name.

    Self-assignments (``X = X``, the re-export idiom) are skipped: they name no
    other class and would otherwise make every chain walk trivially cyclic.
    """
    aliases = import_aliases or {}
    targets: dict[str, str] = {}
    body = tree.body if isinstance(tree, ast.Module) else []
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            bindings = [t for t in stmt.targets if isinstance(t, ast.Name)]
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            bindings = [stmt.target]
            value = stmt.value
        else:
            continue
        if isinstance(value, ast.Name):
            target = value.id
        elif isinstance(value, ast.Attribute):
            target = value.attr
        else:
            continue
        for binding in bindings:
            if binding.id != target:
                targets.setdefault(binding.id, aliases.get(target, target))
    return targets


def register_alias_records(
    by_name: dict[str, _RecordT], alias_targets: dict[str, str]
) -> None:
    """Point every alias name in *alias_targets* at the record it ultimately names.

    Mutates *by_name* in place, so one call fixes every lookup that already goes
    through the registry rather than each call site separately.

    * **A real declaration always wins.**  Insertion is guarded on ``name not in
      by_name``, matching the registry's existing first-wins semantics, so an
      alias can never shadow a class of the same name.
    * **Alias-to-alias hops are followed**, which covers a re-export chain across
      modules; a cycle stops at the name that started the walk.
    * **An unproven chain is left out.**  An alias whose chain never reaches a
      registered record gets no entry, so a genuinely unresolvable name still
      reports as unresolvable instead of being silently waved through.
    """
    for name, first in alias_targets.items():
        if name in by_name:
            continue
        seen = {name}
        target = first
        while target not in by_name and target in alias_targets and target not in seen:
            seen.add(target)
            target = alias_targets[target]
        record = by_name.get(target)
        if record is not None:
            by_name[name] = record


def _module_of(rel_file: str) -> str:
    """Dotted module name of a repo-relative ``.py`` path (``src/`` layout aware)."""
    parts = rel_file.replace("\\", "/").removesuffix(".py").split("/")
    if parts[:1] == ["src"]:
        parts = parts[1:]
    if parts[-1:] == ["__init__"]:
        parts = parts[:-1]
    return ".".join(parts)


def _imported_module(node: ast.ImportFrom, rel_file: str) -> str | None:
    if node.level == 0:
        return node.module or None
    package = _module_of(rel_file).split(".")
    if not rel_file.replace("\\", "/").endswith("/__init__.py"):
        package = package[:-1]
    if node.level - 1 > len(package):
        return None
    package = package[: len(package) - (node.level - 1)]
    return ".".join([*package, *([node.module] if node.module else [])]) or None


def qualified_rebound_imports(
    trees: dict[str, ast.AST],
) -> dict[str, dict[str, str]]:
    """Per file, ``{local: class_name}`` for names imported from an in-repo rebinding.

    A pkl-generated entrypoint module ends with a rebinding of a shared name
    (``AppInputContract = MinerAppInputContract``), and every entrypoint module
    binds that same name to its own class. An app importing it
    (``from app.generated.miner._input import AppInputContract as _Base``) means
    the class *that* module binds. Resolving the import by its bare name instead
    lands on whichever module's rebinding was registered first, so a miner
    contract was resolved, and recorded in the ledger, with the crawler's fields.

    *trees* maps repo-relative paths to parsed modules. Only ``from M import N``
    where ``N`` reaches, inside ``M``, a rebinding to a different name is
    returned. A chain of rebindings inside ``M`` is followed, and so is a
    re-export: ``from app.generated.miner import AppInputContract`` where
    ``miner/__init__.py`` itself imports it from ``._input``.
    """
    rebinds: dict[str, dict[str, str]] = {}
    reexports: dict[str, dict[str, tuple[str, str]]] = {}
    for rel, tree in trees.items():
        module = _module_of(rel)
        rebinds[module] = collect_module_alias_targets(tree)
        imported: dict[str, tuple[str, str]] = {}
        for node in tree.body if isinstance(tree, ast.Module) else []:
            if not isinstance(node, ast.ImportFrom):
                continue
            source = _imported_module(node, rel)
            if source is None:
                continue
            for alias in node.names:
                imported[alias.asname or alias.name] = (source, alias.name)
        reexports[module] = imported

    def resolve(module: str, name: str, seen: set[tuple[str, str]]) -> str | None:
        """The class *name* ends at in *module*, or None when no rebinding is crossed."""
        if (module, name) in seen or module not in rebinds:
            return None
        seen.add((module, name))
        targets = rebinds[module]
        if name in targets:
            target, local_seen = targets[name], {name}
            while target in targets and target not in local_seen:
                local_seen.add(target)
                target = targets[target]
            if target in reexports[module]:
                source, original = reexports[module][target]
                return resolve(source, original, seen) or original
            return target
        if name in reexports[module]:
            source, original = reexports[module][name]
            return resolve(source, original, seen)
        return None

    out: dict[str, dict[str, str]] = {}
    for rel, tree in trees.items():
        resolved: dict[str, str] = {}
        for node in tree.body if isinstance(tree, ast.Module) else []:
            if not isinstance(node, ast.ImportFrom):
                continue
            module = _imported_module(node, rel)
            if module is None:
                continue
            for alias in node.names:
                target = resolve(module, alias.name, set())
                if target is not None and target != alias.name:
                    resolved[alias.asname or alias.name] = target
        if resolved:
            out[rel] = resolved
    return out


def merge_alias_targets(
    per_file: list[dict[str, str]],
) -> tuple[dict[str, str], set[str]]:
    """Merge per-module rebindings into one bare-name map, refusing to guess.

    Returns ``(targets, ambiguous)``. A name two modules rebind to *different*
    classes (each generated entrypoint module binds ``AppInputContract`` to its
    own) has no single meaning, so it is left out of *targets* and returned in
    *ambiguous* instead. Keeping the first rebinding parsed is what attributed
    one entrypoint's fields to another; an unresolved name is visible, a wrong
    one is not. An import that names the module (see
    :func:`qualified_rebound_imports`) never reaches this map.
    """
    seen: dict[str, set[str]] = {}
    for targets in per_file:
        for local, target in targets.items():
            seen.setdefault(local, set()).add(target)
    ambiguous = {local for local, targets in seen.items() if len(targets) > 1}
    merged = {
        local: next(iter(targets))
        for local, targets in seen.items()
        if local not in ambiguous
    }
    return merged, ambiguous
