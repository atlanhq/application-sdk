"""Tests for the shared AST-source discovery walk (``_ast_common.discover``).

Covers the exclusion-path regression: matching against the full (often
absolute) path instead of components relative to the scan root silently
under-scans any repo checked out under a directory whose name happens to
collide with an excluded name (``test``, ``tests``, ``docs``, ``build``,
``dist``, ``conformance``, …).
"""

from __future__ import annotations

import pathlib

from conformance.suite.checks._ast_common import discover


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
