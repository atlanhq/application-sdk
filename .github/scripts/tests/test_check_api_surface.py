"""Tests for check_api_surface.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_api_surface as surface  # noqa: E402

REPO = Path(__file__).resolve().parents[3]

_PYPROJECT = """
[project]
name = "atlan-application-sdk-api"
version = "0.0.0"
dependencies = ["pydantic>=2", "fastapi>=0.1"]
"""


def _tree(tmp_path: Path, files: dict[str, str], listed: list[str]) -> Path:
    (tmp_path / "packages/api").mkdir(parents=True)
    (tmp_path / "packages/api/pyproject.toml").write_text(_PYPROJECT)
    (tmp_path / "packages/api/api-files.txt").write_text(
        "# comment\n" + "\n".join(listed) + "\n"
    )
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


_BASE = {
    "application_sdk/__init__.py": "",
    "application_sdk/handler/__init__.py": "",
    "application_sdk/execution/__init__.py": "import temporalio\n",
    "application_sdk/execution/worker.py": "",
}
_LISTED = [
    "application_sdk/__init__.py",
    "application_sdk/handler/__init__.py",
    "application_sdk/handler/base.py",
]


def _messages(tmp_path: Path, base_py: str) -> list[str]:
    root = _tree(
        tmp_path, {**_BASE, "application_sdk/handler/base.py": base_py}, _LISTED
    )
    return [p.message for p in surface.check(root)]


@pytest.mark.parametrize(
    "src",
    [
        "from application_sdk.handler import x\n",
        "import pydantic\nfrom fastapi import FastAPI\nfrom starlette import status\n",
        "import json\nfrom __future__ import annotations\n",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from application_sdk.execution.worker import W\n",
        "def f():\n"
        "    try:\n"
        "        from application_sdk.execution.worker import W\n"
        "    except ModuleNotFoundError as exc:\n"
        "        if exc.name != 'application_sdk.execution':\n"
        "            raise\n",
        "def f():\n"
        "    try:\n"
        "        import temporalio\n"
        "    except (ImportError, ModuleNotFoundError):\n"
        "        pass\n",
    ],
)
def test_clean(tmp_path: Path, src: str) -> None:
    assert _messages(tmp_path, src) == []


@pytest.mark.parametrize(
    ("src", "fragment"),
    [
        ("from application_sdk.execution.worker import W\n", "not in"),
        ("from application_sdk.execution import worker\n", "not in"),
        ("from ..execution import worker\n", "not in"),
        ("import temporalio\n", "does not declare"),
        (
            "def f():\n    from application_sdk.execution.worker import W\n",
            "without a try/except ModuleNotFoundError",
        ),
        (
            "def f():\n"
            "    try:\n"
            "        from application_sdk.execution.worker import W\n"
            "    except Exception:\n"
            "        pass\n",
            "without a try/except ModuleNotFoundError",
        ),
        ("def f():\n    import temporalio\n", "does not declare"),
    ],
)
def test_flagged(tmp_path: Path, src: str, fragment: str) -> None:
    messages = _messages(tmp_path, src)
    assert len(messages) == 1, messages
    assert fragment in messages[0]


def test_unlisted_parent_package(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {**_BASE, "application_sdk/handler/base.py": ""},
        ["application_sdk/__init__.py", "application_sdk/handler/base.py"],
    )
    (problem,) = surface.check(root)
    assert "parent package application_sdk/handler/__init__.py" in problem.message


def test_missing_listed_file(tmp_path: Path) -> None:
    root = _tree(tmp_path, _BASE, _LISTED)
    (problem,) = surface.check(root)
    assert "application_sdk/handler/base.py does not exist" in problem.message


def test_the_real_api_surface_is_clean() -> None:
    assert [str(p) for p in surface.check(REPO)] == []


def test_the_real_check_catches_a_worker_import(tmp_path: Path) -> None:
    """Mutation check: the real list flags a worker import added to a listed file."""
    listed = surface.listed_files(REPO)
    files = {rel: (REPO / rel).read_text() for rel in listed}
    files["application_sdk/handler/base.py"] += (
        "\nfrom application_sdk.execution import run_dev_combined\n"
    )
    root = _tree(tmp_path, files, listed)
    (tmp_path / "packages/api/pyproject.toml").write_text(
        (REPO / "packages/api/pyproject.toml").read_text()
    )
    (tmp_path / "application_sdk/execution").mkdir(parents=True, exist_ok=True)
    (tmp_path / "application_sdk/execution/__init__.py").write_text("")
    messages = [str(p) for p in surface.check(root)]
    assert any(
        "handler/base.py" in m and "application_sdk.execution" in m for m in messages
    ), messages
