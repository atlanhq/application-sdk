"""Shared helpers for the Temporal-orchestration prescription rules (P004–P007).

BLDX-1417 — apps must reach the orchestration layer **only through the SDK seam**
(``application_sdk.app`` for runtime primitives/decorators, ``application_sdk.execution``
for client/worker/converter), and the SDK must keep Temporal contained *behind*
that seam so the engine can evolve without breaking apps.

These helpers give the four detectors a single, deterministic notion of:

* what counts as a ``temporalio`` import (``iter_temporal_imports``),
* what counts as reaching into SDK-private orchestration internals
  (``private_sdk_target`` / ``private_sdk_import_name``),
* where the blessed boundary is (``in_adapter`` / ``is_primitive_reexport_file``),
* whether a name / annotation resolves to a raw ``temporalio`` type
  (``collect_import_bindings`` / ``origin_is_temporal`` / ``signature_refs_temporal``).
"""

from __future__ import annotations

import ast

TEMPORAL_ROOT = "temporalio"
SDK_TOP = "application_sdk"


# ── Temporal import detection ────────────────────────────────────────────────


def _is_temporal_module(module: str | None) -> bool:
    """True if a dotted module path names ``temporalio`` or a submodule of it."""
    if not module:
        return False
    return module == TEMPORAL_ROOT or module.startswith(TEMPORAL_ROOT + ".")


def iter_temporal_imports(tree: ast.AST) -> list[ast.stmt]:
    """Return every import statement (any depth) that pulls in ``temporalio``.

    Walks the whole tree so lazy/in-function ``import temporalio`` is caught too —
    a ban that only looked at module-level imports would be trivially evaded.
    """
    hits: list[ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_temporal_module(a.name) for a in node.names):
                hits.append(node)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import — never the external temporalio dist.
            if node.level == 0 and _is_temporal_module(node.module):
                hits.append(node)
    return hits


def temporal_import_hint(node: ast.stmt) -> str:
    """Best-effort 'use this instead' hint for a flagged ``temporalio`` import."""
    names: set[str] = set()
    module = ""
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        names = {a.name for a in node.names}
    elif isinstance(node, ast.Import):
        module = next((a.name for a in node.names), "")

    primitives = {
        "now",
        "sleep",
        "uuid4",
        "signal",
        "query",
        "update",
        "wait_condition",
        "workflow",
        "activity",
    }
    if module.startswith("temporalio.client") or "Client" in names:
        return "Use 'from application_sdk.execution import create_temporal_client'."
    if module.startswith("temporalio.worker") or names & {"Worker"}:
        return "Use 'from application_sdk.execution import AppWorker, create_worker'."
    if names & primitives or module in {"temporalio.workflow", "temporalio.activity"}:
        return (
            "Use the re-exports from 'application_sdk.app' "
            "(task, signal, query, update, now, sleep, uuid4, wait_condition)."
        )
    return (
        "Reach orchestration through 'application_sdk.app' / "
        "'application_sdk.execution' instead."
    )


# ── SDK-private orchestration internals (P005) ───────────────────────────────


def private_sdk_target(node: ast.ImportFrom) -> str | None:
    """Return the offending dotted target if *node* reaches SDK-private internals.

    Two shapes are flagged:

    * a private *module* segment under ``application_sdk``
      (``from application_sdk.execution._temporal.worker import create_worker``), and
    * importing a private *name* from a public SDK module
      (``from application_sdk.execution import _something``).

    Returns ``None`` for relative imports, non-SDK modules, and public targets.
    """
    if node.level != 0:
        return None
    module = node.module or ""
    segs = module.split(".")
    if not segs or segs[0] != SDK_TOP:
        return None
    if any(seg.startswith("_") for seg in segs[1:]):
        return module
    private_names = [a.name for a in node.names if a.name.startswith("_")]
    if private_names:
        return f"{module}.{private_names[0]}"
    return None


def private_sdk_import_name(alias_name: str) -> bool:
    """True for ``import application_sdk.<...>._private.<...>`` dotted imports."""
    segs = alias_name.split(".")
    return segs[0] == SDK_TOP and any(seg.startswith("_") for seg in segs[1:])


# ── Blessed boundary (P006) ──────────────────────────────────────────────────


def _norm(file: str) -> str:
    return file.replace("\\", "/")


def in_adapter(file: str) -> bool:
    """True if *file* lives in the orchestration adapter ``execution/_temporal/``.

    The adapter is the one place in the SDK that may import ``temporalio``.
    """
    return "/execution/_temporal/" in "/" + _norm(file)


def is_primitive_reexport_file(file: str) -> bool:
    """True for ``application_sdk/app/__init__.py`` — the curated primitive seam.

    This module deliberately re-exports the runtime primitives apps depend on
    (now/sleep/signal/query/update/wait_condition/uuid4/…), so it is not a leak.
    """
    return _norm(file).endswith("application_sdk/app/__init__.py")


def is_test_file(file: str) -> bool:
    """True if *file* is a test module / harness (a ``tests`` tree or ``test_*``).

    The SDK-scope rules (P006/P007) exempt tests: the SDK's own tests legitimately
    drive the Temporal adapter directly, and a test file is never part of the
    public API surface.  The app-scope rules (P004/P005) deliberately do *not* use
    this — an app's test harness must still go through the SDK seam (BLDX-1417).
    """
    parts = _norm(file).split("/")
    if any(p in {"tests", "test"} for p in parts):
        return True
    name = parts[-1] if parts else file
    return name.startswith("test_") or name.endswith("_test.py")


# ── Raw temporal type references (P007) ───────────────────────────────────────


def origin_is_temporal(origin: str | None) -> bool:
    """True if a resolved import origin (``module.name``) comes from temporalio."""
    return bool(origin) and origin.split(".", 1)[0] == TEMPORAL_ROOT


def collect_import_bindings(tree: ast.AST) -> dict[str, str]:
    """Map each bound name to its fully-qualified import origin (any depth).

    ``import temporalio``                 -> ``{"temporalio": "temporalio"}``
    ``from temporalio.client import Client`` -> ``{"Client": "temporalio.client.Client"}``
    ``from temporalio import workflow``   -> ``{"workflow": "temporalio.workflow"}``
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                bindings[bound] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                continue
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                bindings[bound] = f"{module}.{alias.name}" if module else alias.name
    return bindings


def annotation_refs_temporal(
    annotation: ast.expr | None, bindings: dict[str, str]
) -> bool:
    """True if an annotation expression references a raw ``temporalio`` type."""
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and origin_is_temporal(bindings.get(node.id)):
            return True
    return False


def signature_refs_temporal(
    func: ast.FunctionDef | ast.AsyncFunctionDef, bindings: dict[str, str]
) -> bool:
    """True if any parameter or the return annotation exposes a temporalio type."""
    a = func.args
    params = [
        *a.posonlyargs,
        *a.args,
        *a.kwonlyargs,
        *([a.vararg] if a.vararg else []),
        *([a.kwarg] if a.kwarg else []),
    ]
    if any(annotation_refs_temporal(p.annotation, bindings) for p in params):
        return True
    return annotation_refs_temporal(func.returns, bindings)


def dunder_all_names(tree: ast.Module) -> set[str]:
    """Return the string members of a module-level ``__all__`` assignment."""
    names: set[str] = set()
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            names.update(
                el.value
                for el in value.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            )
    return names


def binding_node_for(tree: ast.Module, name: str) -> ast.stmt | None:
    """Return the top-level import statement that binds *name*, if any."""
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return node
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return node
    return None
