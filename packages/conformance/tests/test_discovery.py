"""Tests for the shared AST-source discovery walk (``_ast_common.discover``).

Covers the exclusion-path regression: matching against the full (often
absolute) path instead of components relative to the scan root silently
under-scans any repo checked out under a directory whose name happens to
collide with an excluded name (``test``, ``tests``, ``docs``, ``build``,
``dist``, ``conformance``, …).
"""

from __future__ import annotations

import pathlib

from conformance.suite.checks import entrypoint, error_seam, orchestration
from conformance.suite.checks._ast_common import (
    discover,
    discover_contract_repo,
    discover_root_file,
    discover_tree,
    discover_with_tests,
)


def test_discover_finds_files_under_root_named_like_excluded_dir(
    tmp_path: pathlib.Path,
) -> None:
    """A scan root whose own path contains an excluded dir name must not blank out."""
    root = tmp_path / "worktrees" / "conformance" / "repo"
    root.mkdir(parents=True)
    (root / "app.py").write_text("x = 1\n")

    found = discover(root)

    assert found == [root / "app.py"]


def test_discover_still_excludes_matching_dirs_inside_root(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "app.py").write_text("x = 1\n")
    (root / "tests" / "test_app.py").write_text("x = 1\n")
    (root / "build" / "generated.py").write_text("x = 1\n")

    found = discover(root)

    assert found == [root / "app.py"]


def test_discover_excludes_dot_prefixed_dirs_under_a_collision_root(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "docs" / "repo"
    root.mkdir(parents=True)
    (root / ".github").mkdir()
    (root / "app.py").write_text("x = 1\n")
    (root / ".github" / "script.py").write_text("x = 1\n")

    found = discover(root)

    assert found == [root / "app.py"]


# ── Named helpers that replaced per-check copies of the walk (FND-4945) ──────


def _touch(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n")
    return path


def test_discover_with_tests_keeps_test_trees_and_files(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "repo"
    app = _touch(root / "app.py")
    test_file = _touch(root / "tests" / "unit" / "test_app.py")
    _touch(root / "build" / "generated.py")
    _touch(root / ".github" / "script.py")

    assert discover_with_tests(root) == [app, test_file]


def test_discover_with_tests_under_a_collision_root(tmp_path: pathlib.Path) -> None:
    """The P-series walkers that keep tests used to match excluded names against
    the absolute path, so a checkout under ``.../build/`` scanned nothing."""
    root = tmp_path / "build" / "repo"
    app = _touch(root / "app.py")

    assert discover_with_tests(root) == [app]
    for module in (entrypoint, error_seam, orchestration):
        assert module.discover(root) == [app], module.__name__


def test_discover_tree_walks_one_subtree(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "repo"
    _touch(root / "app.py")
    unit = _touch(root / "tests" / "unit" / "test_a.py")
    helper = _touch(root / "tests" / "e2e" / "helpers.py")
    _touch(root / "tests" / "__pycache__" / "x.py")

    assert discover_tree(root, "tests") == [helper, unit]
    assert discover_tree(root, "tests", "e2e") == [helper]
    assert discover_tree(root, "missing") == []


def test_discover_root_file(tmp_path: pathlib.Path) -> None:
    assert discover_root_file(tmp_path, "pyproject.toml") == []
    (tmp_path / "pyproject.toml").mkdir()
    assert discover_root_file(tmp_path, "pyproject.toml") == []
    (tmp_path / "pyproject.toml").rmdir()
    pyproject = _touch(tmp_path / "pyproject.toml")
    assert discover_root_file(tmp_path, "pyproject.toml") == [pyproject]


def test_discover_contract_repo(tmp_path: pathlib.Path) -> None:
    assert discover_contract_repo(tmp_path) == []
    (tmp_path / "contract").mkdir()
    assert discover_contract_repo(tmp_path) == [tmp_path]


def test_discover_tree_under_a_pycache_root(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "__pycache__" / "repo"
    test_file = _touch(root / "tests" / "test_a.py")

    assert discover_tree(root, "tests") == [test_file]
