"""Smoke test: every submodule under application_sdk imports cleanly.

Catches the bug class where a renamed/removed import inside a function body
(inline import) goes unnoticed until execution reaches that function in
production. With this test in place, every PR proves at import time that no
top-level reference is broken.

Discovery is file-based (``Path.rglob``) rather than ``pkgutil.walk_packages``
to keep discovery decoupled from import success: if a package's ``__init__.py``
breaks, ``walk_packages`` silently drops every submodule under it, hiding the
regression. File-based discovery surfaces the failure on the broken module
itself.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import application_sdk

_ROOT = Path(application_sdk.__file__).parent


def _discover_modules() -> list[str]:
    """Return every importable submodule path of application_sdk."""
    out: list[str] = []
    for path in sorted(_ROOT.rglob("*.py")):
        rel = path.relative_to(_ROOT)
        if path.name == "__init__.py":
            parts = rel.parent.parts
        else:
            parts = rel.with_suffix("").parts
        if not parts:
            continue
        out.append("application_sdk." + ".".join(parts))
    return sorted(set(out))


_MODULE_NAMES: list[str] = _discover_modules()


@pytest.mark.parametrize("module_name", _MODULE_NAMES)
def test_module_imports_cleanly(module_name: str) -> None:
    """Every submodule of application_sdk must be importable without errors."""
    importlib.import_module(module_name)


def test_smoke_module_count_is_stable() -> None:
    """Floor-guard the discovery count so regressions can't sneak through.

    If this assertion fires after a legitimate module add/remove, update the
    floor — but treat unexpected drops as a hidden regression first.
    """
    expected_min = 100
    assert len(_MODULE_NAMES) >= expected_min, (
        f"Discovered only {len(_MODULE_NAMES)} application_sdk modules; "
        f"expected >= {expected_min}. File-based discovery may be misconfigured."
    )
