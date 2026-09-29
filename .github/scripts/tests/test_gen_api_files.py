"""Tests for gen_api_files.py — api-files.txt is generated from the seeds."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gen_api_files as gen  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


def _tree(tmp_path: Path, files: dict[str, str], seeds: list[str]) -> Path:
    (tmp_path / "packages/api").mkdir(parents=True)
    (tmp_path / "packages/api/pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\ndependencies = []\n\n'
        "[tool.atlan-api]\nseeds = [" + ", ".join(f'"{s}"' for s in seeds) + "]\n"
    )
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


_FILES = {
    "application_sdk/__init__.py": "",
    "application_sdk/handler/__init__.py": "",
    "application_sdk/handler/base.py": "from application_sdk.errors import AppError\n",
    "application_sdk/errors.py": "class AppError(Exception): ...\n",
    "application_sdk/execution.py": "import temporalio\n",
    "application_sdk/handler/lazy.py": (
        "def f():\n"
        "    try:\n"
        "        from application_sdk.execution import x\n"
        "    except ModuleNotFoundError:\n"
        "        pass\n"
        "def g():\n"
        "    from application_sdk.errors import AppError\n"
    ),
}


def test_the_closure_follows_imports_and_parents(tmp_path: Path) -> None:
    root = _tree(tmp_path, _FILES, ["application_sdk.handler.base"])
    assert gen.closure(root, gen.seeds(root)) == [
        "application_sdk/__init__.py",
        "application_sdk/errors.py",
        "application_sdk/handler/__init__.py",
        "application_sdk/handler/base.py",
    ]


def test_a_guarded_lazy_import_is_not_followed_an_unguarded_one_is(
    tmp_path: Path,
) -> None:
    root = _tree(tmp_path, _FILES, ["application_sdk.handler.lazy"])
    files = gen.closure(root, gen.seeds(root))
    assert "application_sdk/execution.py" not in files
    assert "application_sdk/errors.py" in files


def test_check_fails_when_the_list_is_hand_edited(tmp_path: Path) -> None:
    root = _tree(tmp_path, _FILES, ["application_sdk.handler.base"])
    assert gen.main(["--root", str(root)]) == 0
    assert gen.main(["--root", str(root), "--check"]) == 0
    listing = root / "packages/api/api-files.txt"
    listing.write_text(listing.read_text().replace("application_sdk/errors.py\n", ""))
    assert gen.main(["--root", str(root), "--check"]) == 1


def test_the_committed_list_is_current() -> None:
    assert gen.main(["--root", str(REPO), "--check"]) == 0
