"""pytest.ini puts .github/scripts and tests/ on sys.path; no test re-adds them.

Before pytest.ini set ``pythonpath``, 171 test files each inserted one of the
two directories themselves, in a dozen spellings. The guard below resolves
every ``sys.path.insert`` / ``sys.path.append`` in a test file statically and
fails on one that lands on either directory, so the copies cannot drift back
in. An insert for a script kept elsewhere (``.github/actions/*/``,
``build_and_publish/``, …) is still needed and passes.
"""

from __future__ import annotations

import ast
import configparser
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
_COVERED = {SCRIPTS_DIR, TESTS_DIR}


def _resolve(node: ast.expr, names: dict[str, ast.expr], file: Path) -> Path | None:
    """The directory a path expression names, or None if it is not static.

    Understands what the test files actually write: ``Path(__file__)`` and
    ``os.path.dirname(__file__)`` chains through ``.parent``, ``.parents[n]``,
    ``.resolve()``, ``/ "segment"``, ``str(...)``, ``os.path.join(...)``, and
    module-level names bound to such an expression.
    """
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return file
        bound = names.get(node.id)
        return _resolve(bound, names, file) if bound is not None else None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _resolve(node.value, names, file)
        return base.parent if base else None
    if isinstance(node, ast.Subscript):
        value = node.value
        index = node.slice
        if (
            isinstance(value, ast.Attribute)
            and value.attr == "parents"
            and isinstance(index, ast.Constant)
            and isinstance(index.value, int)
        ):
            base = _resolve(value.value, names, file)
            return base.parents[index.value] if base else None
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _resolve(node.left, names, file)
        right = node.right
        if base and isinstance(right, ast.Constant) and isinstance(right.value, str):
            return base / right.value
        return None
    if isinstance(node, ast.Call):
        func = ast.unparse(node.func)
        if func in {"Path", "str", "os.path.abspath", "os.path.realpath"}:
            return _resolve(node.args[0], names, file) if node.args else None
        if func.endswith(".resolve") and isinstance(node.func, ast.Attribute):
            return _resolve(node.func.value, names, file)
        if func == "os.path.dirname" and node.args:
            base = _resolve(node.args[0], names, file)
            return base.parent if base else None
        if func == "os.path.join" and node.args:
            base = _resolve(node.args[0], names, file)
            for part in node.args[1:]:
                if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                    return None
                base = base / part.value if base else None
            return base
    return None


def covered_inserts(source: str, file: Path) -> list[int]:
    """Line numbers of sys.path inserts that add a directory pytest.ini covers."""
    tree = ast.parse(source)
    names: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                names[target.id] = stmt.value
    hits: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = ast.unparse(node.func)
        if func not in {"sys.path.insert", "sys.path.append"}:
            continue
        arg = node.args[-1]
        resolved = _resolve(arg, names, file)
        if resolved is not None and Path(resolved).resolve() in _COVERED:
            hits.append(node.lineno)
    return hits


def test_pytest_ini_puts_the_scripts_and_helpers_on_the_path() -> None:
    parser = configparser.ConfigParser()
    parser.read(SCRIPTS_DIR / "pytest.ini", encoding="utf-8")
    entries = parser["pytest"]["pythonpath"].split()
    assert {(SCRIPTS_DIR / e).resolve() for e in entries} == _COVERED
    assert str(SCRIPTS_DIR) in sys.path
    assert str(TESTS_DIR) in sys.path


def test_no_test_file_re_adds_a_directory_pytest_ini_covers() -> None:
    offenders = {
        path.name: lines
        for path in sorted(TESTS_DIR.glob("*.py"))
        if (lines := covered_inserts(path.read_text(encoding="utf-8"), path))
    }
    assert not offenders, (
        "pytest.ini's pythonpath already puts .github/scripts and its tests/ on "
        f"sys.path; delete these inserts (file: lines): {offenders}"
    )


# Every spelling the suite used before pytest.ini, each of which must be caught.
_COVERED_SPELLINGS = [
    "sys.path.insert(0, str(Path(__file__).parent.parent))",
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))",
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))",
    "sys.path.insert(0, str(Path(__file__).resolve().parent))",
    "sys.path.insert(0, str(Path(__file__).parent))",
    "sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))",
    "_SCRIPTS_DIR = Path(__file__).resolve().parent.parent\n"
    "sys.path.insert(0, str(_SCRIPTS_DIR))",
    "SCRIPTS_DIR = Path(__file__).resolve().parents[1]\n"
    "sys.path.insert(0, str(SCRIPTS_DIR))",
    "sys.path.append(str(Path(__file__).parent.parent))",
]

_OTHER_DIRECTORIES = [
    'sys.path.insert(0, str(Path(__file__).parent.parent / "build_and_publish"))',
    "sys.path.insert(\n"
    '    0, str(Path(__file__).parent.parent.parent / "actions" / "sign-bot-commits")\n'
    ")",
    "REPO_ROOT = Path(__file__).resolve().parents[3]\n"
    "sys.path.insert(0, str(REPO_ROOT))",
    "sys.path.insert(0, str(some_runtime_value))",
]


@pytest.mark.parametrize("source", _COVERED_SPELLINGS)
def test_each_old_spelling_is_caught(source: str) -> None:
    assert covered_inserts(source, TESTS_DIR / "test_x.py")


@pytest.mark.parametrize("source", _OTHER_DIRECTORIES)
def test_an_insert_for_another_directory_passes(source: str) -> None:
    assert covered_inserts(source, TESTS_DIR / "test_x.py") == []
