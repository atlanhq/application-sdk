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


def _importers_of_reexports(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.ImportFrom)
                or node.module != _DEPRECATED_MODULE
            ):
                continue
            for alias in node.names:
                if alias.name not in _OWN_NAMES and not alias.name.startswith("_"):
                    found.append(
                        f"{path.relative_to(root.parent)}:{node.lineno} {alias.name}"
                    )
    return found


def test_no_sdk_module_imports_a_reexport_through_the_deprecated_module() -> None:
    sdk_root = Path(application_sdk.__file__).parent

    assert _importers_of_reexports(sdk_root) == []
