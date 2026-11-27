"""application_sdk's import graph has no cycles, in-function imports included (FND-4260).

Why this exists
---------------
With only module-level imports counted, application_sdk had one cycle; with
the imports written inside function bodies counted too, one strongly connected
component spanned more than a hundred modules. Each in-function import that
closed a cycle was a place where moving the import to the top of its module
would fail at import time, depending on which module loaded first. That made
"circular" the most common reason for an in-function import, often copied to
imports that closed no cycle at all.

This test builds the graph from source, without importing anything, and fails
on any cycle. The edges it counts:

* every ``import`` / ``from ... import`` that runs, wherever it is written:
  module level, function bodies, ``try`` blocks, ``with`` blocks. Only the body
  of ``if TYPE_CHECKING:`` is skipped, since it never runs;
* the package ``__init__`` of every package a dotted name passes through, since
  importing ``a.b.c`` runs ``a`` and ``a.b`` first;
* a name a package serves lazily (its ``_LAZY`` / ``_LAZY_EXPORTS`` map), to
  the module that defines it, since ``from pkg import Name`` imports that
  module there and then;
* ``importlib.import_module("application_sdk.…")`` with a literal name.

``_DEFERRED_CYCLE_EDGES`` lists the in-function imports that still close a
cycle, each with why it is not fixed yet. The list only shrinks: an entry that
no longer exists, or no longer closes a cycle, fails the test so it is removed.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import application_sdk

_ROOT = Path(application_sdk.__file__).parent
_PKG = "application_sdk"

#: In-function imports that still close a cycle: ``(importer, imported)``
#: module pairs. Remove an entry when its cycle is broken; never add one to
#: get a new cycle past this test. Break the cycle instead: move the shared
#: piece down to a module both sides can import, or pass the dependency in.
_DEFERRED_CYCLE_EDGES: dict[tuple[str, str], str] = {
    (
        "application_sdk.app.base",
        "application_sdk.execution._temporal.activities",
    ): "app.base generates the workflow and activity classes; moving that into execution._temporal is FND-4263",
    (
        "application_sdk.execution._temporal.preflight_gate",
        "application_sdk.execution._temporal.activities",
    ): "the gate glue sits between app.base and the activities; FND-4263 moves it",
    (
        "application_sdk.credentials.utils",
        "application_sdk.app.base",
    ): "the run's credential-directory registry lives in app.base, which imports the credentials package; FND-4263 splits app.base",
    (
        "application_sdk.contracts.base",
        "application_sdk.app.registry",
    ): "contract validation looks up the running app; the registry validates contracts from this module",
    (
        "application_sdk.contracts.base",
        "application_sdk.app",
    ): "the same import: app.registry runs the app package __init__, which binds app.task (it imports contracts.base)",
    (
        "application_sdk.errors.base",
        "application_sdk.errors.wire",
    ): "AppError.to_failure_details builds the wire model, which validates with errors.base's redaction helpers",
    (
        "application_sdk.infrastructure._dapr.http",
        "application_sdk.infrastructure._dapr.client",
    ): "the HTTP client reads the secret-absent classifier from the Dapr client, which wraps it",
    (
        "application_sdk.infrastructure.secrets",
        "application_sdk.infrastructure._dapr",
    ): "the secret store calls the Dapr client, which implements its protocol",
    (
        "application_sdk.infrastructure.secrets",
        "application_sdk.infrastructure._dapr.client",
    ): "as above",
    (
        "application_sdk.dev._runner",
        "application_sdk.main",
    ): "main re-exports run_dev_combined, which runs main's combined mode",
    (
        "application_sdk.storage.chunked",
        "application_sdk.storage.ops",
    ): "ops re-exports download_file_chunked; both need the store resolution and error mapping in ops",
    (
        "application_sdk.storage.integrity",
        "application_sdk.storage.ops",
    ): "ops validates every transfer with integrity, which reads and writes sidecars through ops",
    (
        "application_sdk.storage.reference",
        "application_sdk.storage.file_ref_sync",
    ): "reference and file_ref_sync share the FileReference persist / materialize engine",
    (
        "application_sdk.storage.transfer",
        "application_sdk.storage.reference",
    ): "transfer and reference share the directory persist engine",
    (
        "application_sdk.validation.ndjson",
        "application_sdk.validation.assets",
    ): "ndjson and assets validate each other's records (FND-4264 notes the pair)",
}

#: Ceiling on in-function import statements in application_sdk (FND-4260).
#: Lower it when you lift imports; raising it needs a reason in the PR.
_MAX_IN_FUNCTION_IMPORTS = 648


@dataclass(frozen=True)
class _Edge:
    importer: str
    imported: str
    line: int
    in_function: bool


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _discover() -> dict[str, Path]:
    return {_module_name(p): p for p in sorted(_ROOT.rglob("*.py"))}


_MODULES = _discover()


def _is_package(module: str) -> bool:
    return _MODULES[module].name == "__init__.py"


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _lazy_map(module: str) -> dict[str, str]:
    """``name -> defining module`` for a package that serves names lazily."""
    if not _is_package(module):
        return {}
    tree = ast.parse(_MODULES[module].read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        if target not in ("_LAZY", "_LAZY_EXPORTS") or not isinstance(value, ast.Dict):
            continue
        for key, val in zip(value.keys, value.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if isinstance(val, ast.Tuple) and val.elts:
                val = val.elts[0]
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                name = val.value
                if name.startswith("."):
                    name = module + name if name != "." else module
                out[key.value] = name
    return out


_LAZY_MAPS = {m: _lazy_map(m) for m in _MODULES}


def _with_ancestors(module: str) -> Iterator[str]:
    parts = module.split(".")
    for i in range(1, len(parts) + 1):
        yield ".".join(parts[:i])


def _from_base(importer: str, node: ast.ImportFrom) -> str:
    """The absolute package or module a ``from`` import reads from."""
    base = node.module or ""
    if not node.level:
        return base
    here = importer.split(".")
    if not _is_package(importer):
        here = here[:-1]
    here = here[: len(here) - (node.level - 1)]
    return ".".join([*here, base] if base else here)


def _targets(importer: str, node: ast.Import | ast.ImportFrom) -> set[str]:
    """Modules of application_sdk that *node*, in *importer*, runs or reads.

    The importer's own ancestor packages are already initialising when it runs,
    so the ancestor chain of a dotted name is not an edge out of it. An explicit
    ``from <ancestor> import name`` is, unless every name is a submodule: it
    reads the ancestor's namespace, which is only complete once its
    ``__init__`` has finished.
    """
    out: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            out.update(_with_ancestors(alias.name))
    else:
        base = _from_base(importer, node)
        out.update(_with_ancestors(base))
        reads_namespace = False
        for alias in node.names:
            sub = f"{base}.{alias.name}"
            if sub in _MODULES:
                out.add(sub)
                continue
            reads_namespace = True
            lazy = _LAZY_MAPS.get(base, {}).get(alias.name)
            if lazy is not None:
                out.update(_with_ancestors(lazy))
        if reads_namespace and importer.startswith(base + "."):
            out.add(f"{base}!")  # marker: keep this ancestor edge
    own = {t for t in out if importer.startswith(t + ".")}
    kept = {t[:-1] for t in out if t.endswith("!")}
    return {t for t in out if not t.endswith("!") and (t not in own or t in kept)}


def _literal_import_module(node: ast.Call) -> str | None:
    func = node.func
    name = (
        func.attr
        if isinstance(func, ast.Attribute)
        else func.id
        if isinstance(func, ast.Name)
        else None
    )
    if name != "import_module" or not node.args:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _edges_of(module: str) -> Iterator[_Edge]:
    tree = ast.parse(_MODULES[module].read_text(encoding="utf-8"))

    def keep(target: str) -> bool:
        return target in _MODULES and target != module

    def walk(node: ast.AST, in_function: bool) -> Iterator[_Edge]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for other in child.orelse:
                    yield from walk(other, in_function)
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for target in _targets(module, child):
                    if keep(target):
                        yield _Edge(module, target, child.lineno, in_function)
            elif isinstance(child, ast.Call):
                literal = _literal_import_module(child)
                if literal and literal.startswith(_PKG):
                    for target in _with_ancestors(literal):
                        if keep(target) and not module.startswith(target + "."):
                            yield _Edge(module, target, child.lineno, in_function)
            yield from walk(
                child,
                in_function
                or isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                ),
            )

    yield from walk(tree, False)


def _all_edges() -> list[_Edge]:
    return [edge for module in _MODULES for edge in _edges_of(module)]


def _graph(edges: list[_Edge], drop: set[tuple[str, str]]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {m: set() for m in _MODULES}
    for edge in edges:
        if (edge.importer, edge.imported) not in drop:
            graph[edge.importer].add(edge.imported)
    return graph


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components of more than one module (Tarjan, iterative)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work: list[tuple[str, Iterator[str]]] = [(root, iter(sorted(graph[root])))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph[child]))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    found.append(sorted(component))
    return found


def _path(graph: dict[str, set[str]], start: str, goal: str) -> list[str] | None:
    previous: dict[str, str | None] = {start: None}
    queue = [start]
    for node in queue:
        if node == goal:
            path = [node]
            while (step := previous[path[-1]]) is not None:
                path.append(step)
            return path[::-1]
        for child in sorted(graph[node]):
            if child not in previous:
                previous[child] = node
                queue.append(child)
    return None


def _describe(component: list[str], edges: list[_Edge], drop: set) -> str:
    members = set(component)
    lines = [f"cycle of {len(component)} modules; edges inside it:"]
    for edge in sorted(edges, key=lambda e: (e.importer, e.line)):
        if (
            edge.importer in members
            and edge.imported in members
            and (edge.importer, edge.imported) not in drop
        ):
            where = "in a function" if edge.in_function else "module level"
            lines.append(f"  {edge.importer}:{edge.line} -> {edge.imported} ({where})")
    return "\n".join(lines)


_EDGES = _all_edges()


def test_import_graph_has_no_cycles() -> None:
    drop = set(_DEFERRED_CYCLE_EDGES)
    cycles = _cycles(_graph(_EDGES, drop))
    assert not cycles, (
        "application_sdk has an import cycle (in-function imports count). "
        "Break it rather than deferring the import; see "
        "tests/unit/test_import_cycles.py.\n\n"
        + "\n\n".join(_describe(c, _EDGES, drop) for c in cycles)
    )


def test_every_deferred_cycle_edge_still_closes_a_cycle() -> None:
    drop = set(_DEFERRED_CYCLE_EDGES)
    in_function = {(e.importer, e.imported) for e in _EDGES if e.in_function}
    reduced = _graph(_EDGES, drop)
    stale = []
    for importer, imported in sorted(drop):
        if (importer, imported) not in in_function:
            stale.append(f"{importer} -> {imported}: no in-function import any more")
        elif _path(reduced, imported, importer) is None:
            stale.append(f"{importer} -> {imported}: closes no cycle any more")
    assert not stale, (
        "Remove these entries from _DEFERRED_CYCLE_EDGES (and lift the import "
        "to module level if nothing else keeps it in the function):\n  "
        + "\n  ".join(stale)
    )


def _in_function_imports() -> int:
    count = 0
    for path in _MODULES.values():

        def walk(node: ast.AST, in_function: bool) -> int:
            found = 0
            for child in ast.iter_child_nodes(node):
                if in_function and isinstance(child, (ast.Import, ast.ImportFrom)):
                    found += 1
                found += walk(
                    child,
                    in_function
                    or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)),
                )
            return found

        count += walk(ast.parse(path.read_text(encoding="utf-8")), False)
    return count


def test_in_function_import_count_stays_under_the_ceiling() -> None:
    count = _in_function_imports()
    assert count <= _MAX_IN_FUNCTION_IMPORTS, (
        f"application_sdk has {count} in-function imports, above the ceiling of "
        f"{_MAX_IN_FUNCTION_IMPORTS}. Import at module level; only an optional "
        "dependency or a measured import-time cost justifies deferring one."
    )


def test_the_graph_sees_a_known_edge() -> None:
    """Guard against a parser change that silently finds no edges at all."""
    pairs = {(e.importer, e.imported) for e in _EDGES}
    assert ("application_sdk.storage.ops", "application_sdk.storage.errors") in pairs
    assert len(_EDGES) > 1000


def _module_level_names(module: str) -> set[str]:
    """Names a module binds at module level outside ``if TYPE_CHECKING:``."""
    tree = ast.parse(_MODULES[module].read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom | ast.Import):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def test_no_lazy_name_is_shadowed_by_a_submodule() -> None:
    """A lazily served name that is also a submodule's name must be bound eagerly.

    Importing ``pkg.name`` sets the package attribute ``name`` to the submodule,
    and an attribute found on the module is never routed through
    ``__getattr__``. So once anything imports the submodule, ``from pkg import
    name`` returns the module, not the function: ``@task`` raised "'module'
    object is not callable" in connector apps (FND-4260).
    """
    shadowed = []
    for package, lazy in _LAZY_MAPS.items():
        if not lazy:
            continue
        eager = _module_level_names(package)
        for name in sorted(lazy):
            if f"{package}.{name}" in _MODULES and name not in eager:
                shadowed.append(f"{package}.{name}")
    assert not shadowed, (
        "Bind these at module level in the package __init__ instead of serving "
        "them lazily; a submodule of the same name shadows them:\n  "
        + "\n  ".join(shadowed)
    )


def test_app_task_and_entrypoint_are_the_decorators_after_their_submodules_load() -> (
    None
):
    import subprocess
    import sys

    code = (
        "import application_sdk.app.task, application_sdk.app.entrypoint\n"
        "from application_sdk.app import entrypoint, task\n"
        "assert callable(task) and callable(entrypoint), (task, entrypoint)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-2000:]
