"""Shared helpers for the conformance package's own tests.

Import from here instead of copying a helper out of the nearest test file::

    from tests._helpers import active_ids, scan_files, write_py

``ids`` and ``active_ids`` are deliberately separate names: ``active_ids``
drops suppressed findings (what the runner gates on), ``ids`` keeps them. A
test that asserts ``== []`` says which one it means by the name it calls.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeVar

import pytest

_F = TypeVar("_F")


class _HasRuleId(Protocol):
    @property
    def rule_id(self) -> str: ...

    @property
    def suppressed(self) -> bool: ...


def ids(findings: Iterable[_HasRuleId]) -> list[str]:
    """Rule IDs of every finding, suppressed ones included, in order found."""
    return [f.rule_id for f in findings]


def active_ids(findings: Iterable[_HasRuleId]) -> list[str]:
    """Rule IDs of unsuppressed findings, in order found (the runner gate's view)."""
    return [f.rule_id for f in findings if not f.suppressed]


def write_py(tmp_path: Path, py_files: dict[str, str]) -> list[Path]:
    """Write each ``{relative name: source}`` under *tmp_path*; return the paths."""
    paths: list[Path] = []
    for name, src in py_files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        paths.append(p)
    return paths


def scan_files(
    scan_all: Callable[[Sequence[Path], Path], list[_F]],
    tmp_path: Path,
    files: dict[str, str],
) -> list[_F]:
    """Write *files* under *tmp_path* and run the series' cross-file *scan_all* over them."""
    return scan_all(write_py(tmp_path, files), tmp_path)


def uv_lock(sdk_version: str | None) -> str:
    """A minimal uv.lock. ``None`` omits the atlan-application-sdk entry."""
    other = (
        "[[package]]\n"
        'name = "some-dep"\n'
        'version = "1.2.3"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    if sdk_version is None:
        return other
    return (
        f"{other}\n[[package]]\n"
        'name = "atlan-application-sdk"\n'
        f'version = "{sdk_version}"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )


def extract_node(
    args: dict[str, Any] | None = None, *, app_name: str = "myapp"
) -> dict[str, Any]:
    """A minimal root manifest 'extract' node (no depends_on, no downstream refs)."""
    return {
        "activity_name": "execute_workflow",
        "app_name": app_name,
        "inputs": {
            "workflow_type": "MyWorkflow",
            "app_name": app_name,
            "task_queue": "q",
            "args": {} if args is None else args,
        },
    }


def find_sdk_root(start: Path = Path(__file__)) -> Path | None:
    """The monorepo checkout holding *start*, or ``None`` outside one.

    The root is *start* or the first parent of it with both ``application_sdk/__init__.py`` and
    ``packages/conformance/pyproject.toml``.
    """
    resolved = start.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / "application_sdk" / "__init__.py").is_file() and (
            parent / "packages" / "conformance" / "pyproject.toml"
        ).is_file():
            return parent
    return None


def sdk_root(start: Path = Path(__file__)) -> Path:
    """The monorepo root, for drift guards that compare a table against SDK source.

    These tests only run from the monorepo checkout, so a miss means the
    layout changed — exactly when the guard is needed. Under ``CI`` a miss
    fails the test; locally (for example a copy of the package outside the
    monorepo) it skips.
    """
    root = find_sdk_root(start)
    if root is not None:
        return root
    msg = (
        f"monorepo root not found above {start}: no parent holds both "
        "application_sdk/__init__.py and packages/conformance/pyproject.toml"
    )
    if os.environ.get("CI"):
        pytest.fail(f"{msg}. CI runs from the monorepo, so the layout changed.")
    pytest.skip(msg)
