"""The validation package's standing dependency floor (ADR-0020).

No ``pyarrow``, ``pandas`` or ``pandera`` anywhere in ``application_sdk/validation``.
Two reasons, and neither expires:

* **Cost must match the question.** "Is ``START_TIME`` a timestamp?" is answered by
  one parquet footer read. Answering it with a dataframe pays a dataframe to do a
  metadata lookup and drags pandas into the runtime path of callers that only ever
  see JSON — wrong memory profile, wrong CVE footprint. When the parquet validator
  lands it imports ``pyarrow`` **lazily, inside the function**, so a JSON-only
  caller never pays for it; a module-level import would defeat that and is what
  this test catches.
* **pandera stays test-only.** Value ranges, record counts and statistical checks
  are exactly right there and wrong on the runtime path.

Checked statically over the source, so the result does not depend on what an
earlier test in the session happened to import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import application_sdk.validation as validation_pkg

FORBIDDEN = {"pyarrow", "pandas", "pandera"}

_PACKAGE_ROOT = Path(validation_pkg.__file__).parent
_MODULES = sorted(_PACKAGE_ROOT.rglob("*.py"))


def _imported_roots(tree: ast.AST) -> set[str]:
    """Every top-level package name this module imports, at any nesting depth."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_package_has_modules_to_check() -> None:
    """Guard against the glob silently matching nothing and the suite passing."""
    assert len(_MODULES) >= 4


@pytest.mark.parametrize("module", _MODULES, ids=lambda p: p.name)
def test_no_dataframe_dependency(module: Path) -> None:
    found = _imported_roots(ast.parse(module.read_text())) & FORBIDDEN
    assert not found, f"{module.name} imports {sorted(found)} — see ADR-0020"
