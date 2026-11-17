"""Python-source discovery walk shared by every AST-based check series.

The exclusion policy here is universal (not configurable per-repo): it holds for
every app repo that reuses the conformance suite.  Per-repo scope reduction is
done with the runner's ``--exclude`` path prefixes, not by editing this list.
"""

from __future__ import annotations

from pathlib import Path

# Directories excluded from discovery
EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "tests",
        "test",
        "conformance",
        "docs",
        ".tox",
        "site-packages",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "htmlcov",
    }
)

_EXCLUDE_DIRS_WITH_TESTS: frozenset[str] = EXCLUDE_DIRS - {"tests", "test"}


def discover(root: Path, *, exclude_dirs: frozenset[str] = EXCLUDE_DIRS) -> list[Path]:
    """Discover Python source files under *root*, excluding test and infra dirs.

    Two exclusion layers apply universally (not configurable per-repo):

    * **Named infra dirs** — any path component in ``exclude_dirs``, defaulting to
      :data:`EXCLUDE_DIRS` (e.g. ``tests``, ``build``, ``.venv``).
    * **Dot-prefixed dirs** — any path component that starts with ``"."`` (e.g.
      ``.github``, ``.claude``, ``.mothership``).  These are CI/dev/skill
      scaffolding — never shipped application code — and this rule holds for every
      app repo that reuses the conformance suite.

    *exclude_dirs* is a **narrowing seam for a caller that owns a different
    subtree**, not per-repo configuration: the default list holds for every app
    repo and stays the policy.  Its one use today is ``P046``, which governs the
    conformance package's own sources and so must walk them with ``conformance``
    lifted from the set (see ``checks.text_io_encoding.discover``).
    """
    return _walk_python(root, exclude_dirs=exclude_dirs, skip_test_files=True)


def discover_with_tests(root: Path) -> list[Path]:
    """Discover Python sources under *root*, **including** test trees and files.

    The :func:`discover` walk with ``tests``/``test`` lifted from the excluded
    directory names and the ``test_*.py`` / ``*_test.py`` filename rule off.
    For a series whose contract applies to test harnesses as much as to app
    code (the entrypoint, orchestration and error-seam checks).
    """
    return _walk_python(
        root, exclude_dirs=_EXCLUDE_DIRS_WITH_TESTS, skip_test_files=False
    )


def discover_tree(root: Path, *subdir: str) -> list[Path]:
    """Every ``*.py`` under ``root/<subdir…>``, skipping ``__pycache__``.

    No other exclusion applies: a check that walks a fixed tree (``tests/``,
    ``tests/e2e/``) wants every file in it.  ``__pycache__`` is matched relative
    to the walked tree, so a checkout under a ``__pycache__`` directory is still
    scanned.  Returns ``[]`` when the tree is absent.
    """
    base = root.joinpath(*subdir)
    if not base.is_dir():
        return []
    return sorted(
        p for p in base.rglob("*.py") if "__pycache__" not in p.relative_to(base).parts
    )


def discover_root_file(root: Path, name: str) -> list[Path]:
    """``[root / name]`` when that file exists, else ``[]``.

    For a check that reads one fixed file at the repo root (``pyproject.toml``,
    ``Dockerfile``, ``main.py``).
    """
    path = root / name
    return [path] if path.is_file() else []


def discover_contract_repo(root: Path) -> list[Path]:
    """``[root]`` for a contract-driven app repo (one with ``contract/``), else ``[]``.

    For a cross-artifact K-series check that reads fixed files across the repo
    rather than a discovered file set: the single path is the repo itself, and
    an empty list skips the check.
    """
    return [root] if (root / "contract").is_dir() else []


def _walk_python(
    root: Path, *, exclude_dirs: frozenset[str], skip_test_files: bool
) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*.py"):
        # Exclusion checks must use path components *relative to root*, not the
        # full (often absolute) path — otherwise a repo checked out under a
        # directory that happens to match an excluded name (e.g. a worktree at
        # `.../test/`, `.../build/`, or `.../conformance/`) has every file
        # under it silently dropped from AST-based scanning.
        rel_parts = path.relative_to(root).parts
        dir_parts = rel_parts[:-1]
        # Exclude named infra / virtualenv dirs
        if set(dir_parts) & exclude_dirs:
            continue
        # Exclude any dot-prefixed directory component (.github, .claude, …)
        if any(p.startswith(".") for p in dir_parts):
            continue
        # Exclude test files by name convention
        name = path.name
        if skip_test_files and (name.startswith("test_") or name.endswith("_test.py")):
            continue
        paths.append(path)
    return sorted(paths)
