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
import subprocess
import sys
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


def test_importing_the_package_does_not_load_pyatlan() -> None:
    """The asset model is paid for by asset callers, not by importing the package.

    ``assets.py`` and ``asset_rules.py`` import ``pyatlan_v9`` at module scope and
    must — they are the typed-model check. The floor is that the package
    ``__init__`` serves their names lazily (PEP 562), so a caller that only wants
    the artifact wrapper or the interceptor never loads the asset model. That is
    ~140 MiB of resident memory on every worker that registers activities, because
    ``create_activity_from_task`` imports ``validation.interceptor``.

    Run in a subprocess: whether ``pyatlan_v9`` is warm in this interpreter depends
    on what an earlier test imported, and this must assert the import path itself.
    """
    probe = (
        "import sys;"
        "import application_sdk.validation;"
        "print('pyatlan_v9' in sys.modules or 'pyatlan' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False", (
        "importing application_sdk.validation loaded the pyatlan asset model; "
        "something in the package __init__ reaches assets/asset_rules eagerly"
    )


def test_every_lazy_name_still_resolves() -> None:
    """Deferral must not quietly drop a name from the package's public surface."""
    missing = [
        name for name in validation_pkg.__all__ if not hasattr(validation_pkg, name)
    ]
    assert not missing, f"{missing} are in __all__ but not reachable"


def test_the_asset_submodules_are_still_package_attributes() -> None:
    """``import ...validation as v; v.assets`` keeps working for existing callers.

    Going lazy removes the eager ``from .assets import ...`` that incidentally bound
    ``assets`` and ``asset_rules`` as attributes of the package. Downstream apps that
    reach them that way would get an AttributeError, so ``lazy_getattr`` is given them
    as *submodules* — imported on access, which keeps the floor low and the attribute
    available.

    Subprocess, because importing the submodule anywhere in this interpreter binds it
    on the parent package and would make this pass for the wrong reason.
    """
    for sub in ("assets", "asset_rules"):
        probe = (
            f"import application_sdk.validation as v;getattr(v, {sub!r});print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True
        )
        assert result.returncode == 0 and result.stdout.strip() == "ok", (
            f"application_sdk.validation.{sub} is no longer reachable as an "
            f"attribute: {result.stderr.strip()[-300:]}"
        )
