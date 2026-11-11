"""``tests/unit`` mirrors ``application_sdk``: every directory holding tests names a package directory.

Ownership and per-package coverage are read off the directory, so a test
directory with no package counterpart (``tests/unit/api`` testing
``handler``, ``tests/unit/runtime`` testing ``_runtime``) puts its tests
under the wrong owner. Put a new test next to the module it tests:
``application_sdk/a/b/c.py`` → ``tests/unit/a/b/test_c.py``.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_UNIT = _REPO / "tests" / "unit"
_PACKAGE = _REPO / "application_sdk"

# Directories that hold tests but deliberately have no package directory of
# the same path. Each entry says what it tests instead.
_NOT_MIRRORED: dict[str, str] = {
    "storage/formats/readers": "reader halves of storage/formats/{json,parquet}.py",
    "storage/formats/writers": "writer halves of storage/formats/{json,parquet}.py",
    "tools/test_codemods": "the repo-root tools/migrate_v3/codemods, not application_sdk",
}


# pytest's default ``python_files`` (pyproject sets no override): a directory
# holding either form is collected, so either form must be mirrored.
_TEST_FILE_GLOBS = ("test_*.py", "*_test.py")


def _test_dirs() -> set[str]:
    return {
        path.parent.relative_to(_UNIT).as_posix()
        for glob in _TEST_FILE_GLOBS
        for path in _UNIT.rglob(glob)
        if "__pycache__" not in path.parts
    }


def test_every_unit_test_directory_mirrors_a_package_directory() -> None:
    orphans = sorted(
        rel
        for rel in _test_dirs()
        if rel != "." and rel not in _NOT_MIRRORED and not (_PACKAGE / rel).is_dir()
    )
    assert not orphans, (
        "tests/unit directories with no application_sdk counterpart: "
        f"{orphans}. Move the tests to the directory that mirrors the "
        "package they test."
    )


def test_not_mirrored_entries_still_exist() -> None:
    """A stale allowlist entry would silently re-admit that path."""
    stale = sorted(rel for rel in _NOT_MIRRORED if rel not in _test_dirs())
    assert not stale, f"remove stale _NOT_MIRRORED entries: {stale}"
