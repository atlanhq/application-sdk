"""The validation package's standing dependency floor (ADR-0020).

Two reasons, and neither expires:

* **Cost must match the question.** "Is ``START_TIME`` a timestamp?" is answered by
  one parquet footer read. Answering it with a dataframe pays a dataframe to do a
  metadata lookup and drags pandas into the runtime path of callers that only ever
  see JSON — wrong memory profile, wrong CVE footprint.
* **pandera stays test-only.** Value ranges, record counts and statistical checks
  are exactly right there and wrong on the runtime path.

So the floor has two tiers, and the distinction is the whole point:

* ``pandas`` and ``pandera`` are forbidden **anywhere**, at any nesting depth.
* ``pyarrow`` is forbidden **at module scope**, in every module — which is what
  keeps it off a JSON-only caller's import path. The parquet validator imports it
  *inside* the function that reads a footer, and that is the only place in the
  package allowed to name it at all.

Checked statically over the source, so the result does not depend on what an
earlier test in the session happened to import — and, for the ``pyarrow`` tier, so
that "it was lazy" is asserted against the import's *position in the source* rather
than against whether some import happened to be warm.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import application_sdk.validation as validation_pkg

FORBIDDEN_ANYWHERE = {"pandas", "pandera"}
"""Never imported, at any depth. There is no lazy-import escape hatch for these."""

FORBIDDEN_AT_MODULE_SCOPE = FORBIDDEN_ANYWHERE | {"pyarrow"}
"""Never imported at module scope — that is what "off the import path" means."""

LAZY_PYARROW_MODULES = {"parquet.py"}
"""The only modules permitted to name ``pyarrow`` at all, and then only lazily.

Adding a module here is a design decision, not a formality: every entry is another
place a caller can accidentally pay for a parquet reader.
"""

_PACKAGE_ROOT = Path(validation_pkg.__file__).parent
_MODULES = sorted(_PACKAGE_ROOT.rglob("*.py"))


def _import_roots(node: ast.AST) -> set[str]:
    """Top-level package names imported by this one statement."""
    if isinstance(node, ast.Import):
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        return {node.module.split(".")[0]}
    return set()


def _all_imported_roots(tree: ast.AST) -> set[str]:
    """Every top-level package name imported anywhere, at any nesting depth."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        roots |= _import_roots(node)
    return roots


def _module_scope_imported_roots(tree: ast.Module) -> set[str]:
    """Every package name imported when the module itself is imported.

    Walks the module body and everything that executes with it — ``if``/``try``
    blocks and class bodies — but stops at a function boundary, because that is
    precisely the line a lazy import is placed on the far side of.
    """
    roots: set[str] = set()
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        roots |= _import_roots(node)
        stack.extend(ast.iter_child_nodes(node))
    return roots


def test_the_package_has_modules_to_check() -> None:
    """Guard against the glob silently matching nothing and the suite passing."""
    assert len(_MODULES) >= 4


def test_the_lazy_pyarrow_allowlist_names_real_modules() -> None:
    """A renamed module must not silently turn its allowlist entry into a pass."""
    names = {module.name for module in _MODULES}
    assert LAZY_PYARROW_MODULES <= names


@pytest.mark.parametrize("module", _MODULES, ids=lambda p: p.name)
def test_no_dataframe_dependency_anywhere(module: Path) -> None:
    """pandas and pandera never appear, lazily or otherwise."""
    found = _all_imported_roots(ast.parse(module.read_text())) & FORBIDDEN_ANYWHERE
    assert not found, f"{module.name} imports {sorted(found)} — see ADR-0020"


@pytest.mark.parametrize("module", _MODULES, ids=lambda p: p.name)
def test_nothing_heavy_is_imported_at_module_scope(module: Path) -> None:
    """The import-path guarantee: importing this package loads no parquet reader."""
    found = (
        _module_scope_imported_roots(ast.parse(module.read_text()))
        & FORBIDDEN_AT_MODULE_SCOPE
    )
    assert not found, (
        f"{module.name} imports {sorted(found)} at module scope — it must be "
        f"deferred into the function that needs it (ADR-0020)"
    )


@pytest.mark.parametrize("module", _MODULES, ids=lambda p: p.name)
def test_only_the_allowlisted_modules_name_pyarrow(module: Path) -> None:
    """Lazy or not, pyarrow is confined to the modules that own the parquet path."""
    if module.name in LAZY_PYARROW_MODULES:
        return
    found = _all_imported_roots(ast.parse(module.read_text())) & {"pyarrow"}
    assert not found, (
        f"{module.name} imports pyarrow; only {sorted(LAZY_PYARROW_MODULES)} may, "
        f"and then only inside a function (ADR-0020)"
    )
