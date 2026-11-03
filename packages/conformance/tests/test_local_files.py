"""Tests for the shared worker-disk plumbing (``suite.checks._local_files``).

The SDK helper lists are re-derived from the installed SDK so a new re-export
fails here instead of going unmatched, and the flow primitives the BLOCK rules
lean on — what counts as the same path, a removal, an escape — are pinned
directly, independent of any one rule's message.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
from conformance.suite.checks._local_files import (
    DEV_TOOL_DIRS,
    SDK_STORAGE_MODULES,
    ModuleContext,
    ScopeFlow,
    StorageBindings,
    runs_on_worker,
    same_store,
)


def _installed_bindings() -> dict[str, set[str]]:
    """``{helper: {module, ...}}`` for every top-level binding in the SDK."""
    spec = importlib.util.find_spec("application_sdk")
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("application_sdk is not installed")
    root = Path(next(iter(spec.submodule_search_locations)))
    found: dict[str, set[str]] = {name: set() for name in SDK_STORAGE_MODULES}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parts = path.relative_to(root.parent).with_suffix("").parts
        module = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        for node in tree.body:
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [alias.asname or alias.name for alias in node.names]
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names = [node.name]
            for name in names:
                if name in found:
                    found[name].add(module)
    return found


@pytest.mark.parametrize("helper", sorted(SDK_STORAGE_MODULES))
def test_sdk_storage_modules_cover_every_installed_binding(helper: str) -> None:
    """Drift guard: a module that starts binding a helper at top level is a new
    way to reach the SDK function, and the rules must learn it."""
    installed = _installed_bindings()[helper]
    assert installed, f"found no module binding {helper} — the walk is broken"
    assert installed <= SDK_STORAGE_MODULES[helper], sorted(
        installed - SDK_STORAGE_MODULES[helper]
    )


def _flow(src: str, name: str = "f") -> ScopeFlow:
    tree = ast.parse(src)
    context = ModuleContext.of(tree)
    scope = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == name
    )
    return context.flow(scope)


def test_tokens_follow_path_construction_but_not_data() -> None:
    flow = _flow(
        "import os\n"
        "def f(base, name):\n"
        "    p = os.path.join(base, name)\n"
        "    q = p + '.part'\n"
        "    rows = count_rows(p)\n"
    )
    assert flow.upstream(frozenset({"q"})) >= {"q", "p", "base", "name"}
    assert flow.upstream(frozenset({"rows"})) == {"rows"}


def test_related_covers_ancestors_aliases_and_attributes() -> None:
    flow = _flow(
        "from pathlib import Path\n"
        "def f(out_dir, f):\n"
        "    child = Path(out_dir) / 'x.json'\n"
        "    alias = child\n"
    )
    assert flow.related(frozenset({"child"}), frozenset({"out_dir"}))
    assert flow.related(frozenset({"alias"}), frozenset({"child"}))
    assert flow.related(frozenset({"f.name"}), frozenset({"f"}))
    assert not flow.related(frozenset({"child"}), frozenset({"unrelated"}))


def test_siblings_under_one_base_are_not_related() -> None:
    flow = _flow(
        "import os\n"
        "def f(base):\n"
        "    a = os.path.join(base, 'a')\n"
        "    b = os.path.join(base, 'b')\n"
    )
    assert not flow.related(frozenset({"a"}), frozenset({"b"}))


def test_removals_are_recognised_in_their_forms() -> None:
    flow = _flow(
        "import asyncio, os, shutil\n"
        "from os import unlink\n"
        "from pathlib import Path\n"
        "async def f(a, b, c, d, e, g):\n"
        "    os.remove(a)\n"
        "    unlink(b)\n"
        "    Path(c).unlink(missing_ok=True)\n"
        "    await asyncio.to_thread(shutil.rmtree, d, ignore_errors=True)\n"
        "    delete_local_files([e])\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        "        shutil.rmtree(g)\n"
    )
    by_target = {next(iter(r.targets)): r.in_finally for r in flow.removals}
    assert by_target == {
        "a": False,
        "b": False,
        "c": False,
        "d": False,
        "e": False,
        "g": True,
    }


def test_a_module_method_named_unlink_on_os_is_not_a_path_method() -> None:
    flow = _flow("import os\ndef f(p):\n    os.unlink(p)\n")
    assert [sorted(r.targets) for r in flow.removals] == [["p"]]


def test_registered_callbacks_and_temp_context_managers_cover_every_exit() -> None:
    flow = _flow(
        "import shutil, tempfile\n"
        "from contextlib import ExitStack\n"
        "def f(d):\n"
        "    with ExitStack() as stack:\n"
        "        stack.callback(shutil.rmtree, d)\n"
        "    with tempfile.TemporaryDirectory() as tmp:\n"
        "        pass\n"
        "    with tempfile.NamedTemporaryFile(delete=False) as kept:\n"
        "        pass\n"
    )
    every_exit = sorted(
        token
        for removal in flow.removals
        if removal.in_finally
        for token in removal.targets
    )
    assert every_exit == ["d", "tmp"]


def test_escapes_through_returns_attributes_and_containers() -> None:
    flow = _flow(
        "from application_sdk.contracts.types import FileReference\n"
        "def f(self, a, b, c, d):\n"
        "    refs = []\n"
        "    refs.append(FileReference.from_local(a))\n"
        "    self.kept = b\n"
        "    total = count(c)\n"
        "    return refs, total\n"
    )
    assert flow.escapes(frozenset({"a"}))
    assert flow.escapes(frozenset({"b"}))
    assert not flow.escapes(frozenset({"c"}))  # only a number derived from it leaves
    assert not flow.escapes(frozenset({"d"}))


def test_module_local_removers_are_summarised() -> None:
    tree = ast.parse(
        "import os\n"
        "def _drop(path):\n"
        "    os.remove(path)\n"
        "def _drop_twice(first, second):\n"
        "    _drop(second)\n"
        "class C:\n"
        "    def wipe(self, target):\n"
        "        os.unlink(target)\n"
    )
    removers = ModuleContext.of(tree).removers
    assert removers["_drop"] == (frozenset({0}), frozenset({"path"}))
    assert removers["_drop_twice"] == (frozenset({1}), frozenset({"second"}))
    assert removers["wipe"] == (frozenset({0}), frozenset({"target"}))


def test_storage_bindings_resolve_only_the_sdk() -> None:
    tree = ast.parse(
        "import application_sdk.storage.batch as batch\n"
        "from application_sdk.storage import upload_file as up\n"
        "from app.storage import download_prefix\n"
        "up('k', p)\n"
        "batch.upload_prefix(d, 'k')\n"
        "download_prefix('k', d)\n"
        "client.upload_file('k', p)\n"
    )
    bindings = StorageBindings(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    resolved = sorted(name for call in calls if (name := bindings.resolve(call)))
    assert resolved == ["upload_file", "upload_prefix"]


def test_same_store() -> None:
    def call(src: str) -> ast.Call:
        node = ast.parse(src).body[0]
        assert isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        return node.value

    assert same_store(call("download_prefix(p, d)"), call("upload_prefix(d, q)"))
    assert same_store(
        call("download_prefix(p, d, store=s)"), call("upload_prefix(d, q, store=s)")
    )
    assert not same_store(
        call("download_prefix(p, d)"), call("upload_prefix(d, q, store=upstream)")
    )


@pytest.mark.parametrize("top", sorted(DEV_TOOL_DIRS))
def test_developer_tooling_directories_do_not_run_on_the_worker(top: str) -> None:
    assert not runs_on_worker(f"{top}/run.py")
    assert runs_on_worker(f"app/{top}/run.py")
    assert runs_on_worker("main.py")
