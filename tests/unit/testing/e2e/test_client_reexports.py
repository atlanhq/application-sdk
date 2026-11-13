"""``testing.e2e.client`` is the deprecated home of ``AEWorkflowClient`` only (FND-4912).

Everything else in its ``__all__`` is a re-export of
:mod:`application_sdk.testing.harness.automation_engine`. Two things must hold
until v4.0:

* the old import path keeps working, and resolves to the canonical object, so
  an ``isinstance`` check or a patch sees one class, not two;
* nothing inside the SDK reaches those types *through* the deprecated module,
  so deleting it with ``AEWorkflowClient`` breaks no SDK importer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import application_sdk
from application_sdk.testing.e2e import client as e2e_client
from application_sdk.testing.harness import automation_engine

_DEPRECATED_MODULE = "application_sdk.testing.e2e.client"

#: The names ``testing.e2e.client`` defines itself, or that are private to it.
#: Anything else imported from it is a re-export with a canonical home.
_OWN_NAMES = {"AEWorkflowClient"}

_REEXPORTS = sorted(set(e2e_client.__all__) - _OWN_NAMES)


@pytest.mark.parametrize("name", _REEXPORTS)
def test_reexport_is_the_canonical_object(name: str) -> None:
    assert getattr(e2e_client, name) is getattr(automation_engine, name)


def test_every_reexport_is_on_the_canonical_module() -> None:
    assert set(_REEXPORTS) <= set(automation_engine.__all__)


def _is_reexport(name: str) -> bool:
    return name not in _OWN_NAMES and not name.startswith("_")


def _absolute_module(node: ast.ImportFrom, module_name: str, is_package: bool) -> str:
    """Resolve ``from .x import y`` against the importing module's own name."""
    if node.level == 0:
        return node.module or ""
    package = module_name if is_package else module_name.rpartition(".")[0]
    for _ in range(node.level - 1):
        package = package.rpartition(".")[0]
    return f"{package}.{node.module}" if node.module else package


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _reexport_uses(
    tree: ast.AST, module_name: str, is_package: bool
) -> list[tuple[int, str]]:
    """Every re-export a module reaches through the deprecated module, any import form.

    ``from <deprecated> import X`` names ``X`` directly. ``import <deprecated>
    [as m]`` and ``from <parent> import client [as m]`` bind the module itself,
    so what it is used for is read off the attribute accesses on that binding.
    """
    parent, _, leaf = _DEPRECATED_MODULE.rpartition(".")
    found: list[tuple[int, str]] = []
    #: Expressions that evaluate to the deprecated module in this file.
    module_refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = _absolute_module(node, module_name, is_package)
            for alias in node.names:
                if source == _DEPRECATED_MODULE and _is_reexport(alias.name):
                    found.append((node.lineno, alias.name))
                elif source == parent and alias.name == leaf:
                    module_refs.add(alias.asname or leaf)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _DEPRECATED_MODULE:
                    module_refs.add(alias.asname or _DEPRECATED_MODULE)
    if module_refs:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and _dotted(node.value) in module_refs
                and _is_reexport(node.attr)
            ):
                found.append((node.lineno, node.attr))
    return found


def _importers_of_reexports(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root.parent)
        is_package = path.name == "__init__.py"
        parts = relative.with_suffix("").parts
        module_name = ".".join(parts[:-1] if is_package else parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, name in _reexport_uses(tree, module_name, is_package):
            found.append(f"{relative}:{lineno} {name}")
    return found


@pytest.mark.parametrize(
    "source",
    [
        "from application_sdk.testing.e2e.client import DAGRunResult",
        "import application_sdk.testing.e2e.client as legacy\nlegacy.DAGRunResult",
        "import application_sdk.testing.e2e.client\n"
        "application_sdk.testing.e2e.client.DAGRunResult",
        "from application_sdk.testing.e2e import client as legacy\nlegacy.DAGRunResult",
        "from application_sdk.testing.e2e import client\nclient.DAGRunResult",
        "from .client import DAGRunResult",
        "from . import client\nclient.DAGRunResult",
        "from ..e2e.client import DAGRunResult",
    ],
)
def test_guard_sees_every_import_form(source: str) -> None:
    """Each form a module could reach a re-export by, from inside ``testing.e2e``."""
    tree = ast.parse(source)

    uses = _reexport_uses(tree, "application_sdk.testing.e2e.base", is_package=False)

    assert [name for _, name in uses] == ["DAGRunResult"]


@pytest.mark.parametrize(
    "source",
    [
        "from application_sdk.testing.e2e.client import AEWorkflowClient",
        "import application_sdk.testing.e2e.client as legacy\nlegacy.AEWorkflowClient",
        "from application_sdk.testing.e2e import client\nclient._settled_or_fail_open",
        "from application_sdk.testing.harness.automation_engine import DAGRunResult",
        "from .base import DAGRunResult",
    ],
)
def test_guard_allows_the_deprecated_modules_own_names(source: str) -> None:
    tree = ast.parse(source)

    assert _reexport_uses(tree, "application_sdk.testing.e2e.base", False) == []


def test_no_sdk_module_imports_a_reexport_through_the_deprecated_module() -> None:
    sdk_root = Path(application_sdk.__file__).parent

    assert _importers_of_reexports(sdk_root) == []
