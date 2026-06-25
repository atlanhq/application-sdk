"""C003 GitignoreMissingEntry — detect absent or incomplete .gitignore.

Bootstrap writes a standard .gitignore when the file is absent.  This check
flags any required entry that is missing from the file.

Two cases:
- ``.gitignore`` absent → one WARN finding; run ``bootstrap`` to create it.
- ``.gitignore`` exists but missing an entry → one WARN finding per entry.

Both cases are WARN only (never BLOCK); the file is app-editable.

Covered entries
---------------
An entry in *required* is considered "covered" when the file contains:

- An exact-match line (``__pycache__/`` covers ``__pycache__/``).
- A no-trailing-slash variant for directory patterns
  (``.venv`` covers ``.venv/``).
- The glob ``**/node_modules/**`` as an equivalent for ``node_modules/``.
"""

from __future__ import annotations

from pathlib import Path

from conformance.suite.schema.findings import Finding

SERIES = "C"
RULE_ID = "C003"

_CLI_CMD = "atlan-application-sdk-conformance bootstrap"

# Canonical required entries in the order they appear in the bootstrap template.
# ``_is_covered`` accepts exact matches and the equivalences documented above.
REQUIRED_ENTRIES: tuple[str, ...] = (
    ".DS_Store",
    ".vscode/",
    ".idea/",
    "*~",
    ".env",
    "__pycache__/",
    ".venv/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".coverage*",
    "htmlcov/",
    "dist/",
    "node_modules/",
    ".atlan/",
    ".claude/worktrees/",
    "remediation/",
)


def _is_covered(required: str, present: frozenset[str]) -> bool:
    """Return True if *present* contains a line that effectively covers *required*."""
    if required in present:
        return True
    # .venv/ is also covered by .venv (gitignore matches both interpretations)
    if required.endswith("/") and required[:-1] in present:
        return True
    # node_modules/ is also covered by the recursive glob form
    if required == "node_modules/" and "**/node_modules/**" in present:
        return True
    return False


def _present_lines(path: Path) -> frozenset[str]:
    """Return the set of non-blank, non-comment, stripped lines in *path*."""
    lines: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            lines.add(stripped)
    return frozenset(lines)


# ---------------------------------------------------------------------------
# Discovery + scanning
# ---------------------------------------------------------------------------


def discover(root: Path) -> list[Path]:
    """Return the single .gitignore path (whether or not it exists)."""
    return [root / ".gitignore"]


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Return C003 findings for the repo .gitignore."""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = str(path)

    if not path.exists():
        return [
            Finding(
                rule_id=RULE_ID,
                file=rel,
                line=1,
                column=1,
                message=(
                    ".gitignore is absent. "
                    f"Run `{_CLI_CMD}` to create it with the standard entries."
                ),
            )
        ]

    present = _present_lines(path)
    findings: list[Finding] = []
    for entry in REQUIRED_ENTRIES:
        if not _is_covered(entry, present):
            findings.append(
                Finding(
                    rule_id=RULE_ID,
                    file=rel,
                    line=1,
                    column=1,
                    message=(
                        f".gitignore is missing the standard entry {entry!r}. "
                        f"Add it manually or run `{_CLI_CMD}` on a new repo to "
                        f"scaffold a .gitignore that includes it."
                    ),
                )
            )
    return findings
