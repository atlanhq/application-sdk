"""Pytest collection-mirror helpers shared by every T-series (test-quality) check.

Every T-series check needs the same answer to "does pytest collect this as a
test?" — T001 (marker checks), and the newer T005–T013 (assertion quality,
silent non-execution, tier structure) all walk the same ``test*``
function / ``Test*`` class / ``test_*.py`` file shapes. Centralising them here
means the collection-mirror logic (and its documented limitation) has exactly
one definition instead of being re-derived per series.

**Documented limitation** (shared by every caller): this mirrors pytest's
*default* collection configuration only. Non-default ``python_files`` /
``python_classes`` / ``python_functions`` overrides in a repo's
``pyproject.toml`` are not honoured — biasing toward simplicity over
per-repo customisation, consistent with the rest of the T-series.
"""

from __future__ import annotations

import ast
from typing import TypeGuard

__all__ = ["is_collectable_test_file", "is_test_class", "is_test_function"]


def is_test_function(
    node: ast.stmt,
) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    """True for a pytest-collected test function (default ``python_functions``)."""
    return isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) and node.name.startswith("test")


def is_test_class(node: ast.stmt) -> TypeGuard[ast.ClassDef]:
    """True for a pytest-collected test class (default ``python_classes``)."""
    return isinstance(node, ast.ClassDef) and node.name.startswith("Test")


def is_collectable_test_file(name: str) -> bool:
    """True for a filename pytest collects by default (``python_files``).

    Accepts ``test_*.py`` and ``*_test.py`` — the two default patterns. Any
    other name under a test-tier directory (e.g. ``connector_tests.py``) is
    never collected, however many ``def test_*`` functions it defines.
    """
    return name.startswith("test_") or name.endswith("_test.py")
