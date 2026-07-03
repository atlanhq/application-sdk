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


def discover(root: Path) -> list[Path]:
    """Discover Python source files under *root*, excluding test and infra dirs.

    Two exclusion layers apply universally (not configurable per-repo):

    * **Named infra dirs** — any path component in ``EXCLUDE_DIRS`` (e.g. ``tests``,
      ``build``, ``.venv``).
    * **Dot-prefixed dirs** — any path component that starts with ``"."`` (e.g.
      ``.github``, ``.claude``, ``.mothership``).  These are CI/dev/skill
      scaffolding — never shipped application code — and this rule holds for every
      app repo that reuses the conformance suite.
    """
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
        if set(dir_parts) & EXCLUDE_DIRS:
            continue
        # Exclude any dot-prefixed directory component (.github, .claude, …)
        if any(p.startswith(".") for p in dir_parts):
            continue
        # Exclude test files by name convention
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        paths.append(path)
    return sorted(paths)
