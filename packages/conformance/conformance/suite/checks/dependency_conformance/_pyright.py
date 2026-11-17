"""D015 — a ``[tool.pyright].exclude`` that clobbers pyright's default excludes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from conformance.suite.checks._ast_common import (
    parse_toml_suppressions,
)
from conformance.suite.schema.findings import Finding

from ._common import (
    RULE_D015,
    _line_of,
    _make_finding,
    _safe_load,
)
from ._index import (
    _table_header_line,
)

#: The built-in ``exclude`` defaults pyright applies when the key is absent.
#: Declaring ``exclude`` replaces this list wholesale rather than extending it.
PYRIGHT_DEFAULT_EXCLUDES = ("**/node_modules", "**/__pycache__", "**/.*")


#: Entries that restore the dot-directory protection the defaults provided,
#: normalised (stripped, no trailing slash) before comparison.  ``**/.*`` is
#: the default itself; the ``.venv`` spellings cover a repo that protects the
#: virtualenv explicitly without restating the glob.  Only this protection is
#: graded — losing ``**/node_modules`` and ``**/__pycache__`` costs nothing in
#: a Python repo, and demanding all three would fail repos that deliberately
#: restate just the one that matters.
_DOTDIR_GUARDS = frozenset(
    {"**/.*", ".venv", ".venv/**", "./.venv", "./.venv/**", "**/.venv", "**/.venv/**"}
)


def _pyright_table(
    text: str, *, data: Mapping[str, Any] | None = None
) -> Mapping[str, Any] | None:
    """Return ``[tool.pyright]`` as a mapping, or ``None`` when absent."""
    if data is None:
        data = _safe_load(text)
    if data is None:
        return None
    tool = data.get("tool")
    pyright = tool.get("pyright") if isinstance(tool, dict) else None
    return pyright if isinstance(pyright, dict) else None


def _scan_pyright_excludes(text: str, rel_pyproject: str) -> list[Finding]:
    """D015: a ``[tool.pyright] exclude`` must keep the dot-directory default.

    The key's *presence* is what destroys the built-in defaults, so an empty
    list is graded exactly like a populated one — ``exclude = []`` reads as a
    no-op and is in fact the worst case, replacing the defaults with nothing.

    A scoped ``include`` is a complete defence and exits early: pyright then
    walks only the listed roots and never reaches ``.venv``, so whatever
    ``exclude`` says cannot matter.  Three fleet repos rely on this, and a rule
    that ignored it would report every one of them.

    ``ignore`` is deliberately not consulted.  It silences diagnostics for the
    files it matches but still parses them, so it does nothing about the walk
    cost that is the whole reason this rule exists.
    """
    pyright = _pyright_table(text)
    if pyright is None:
        return []
    if "include" in pyright:
        return []
    excludes = pyright.get("exclude")
    if not isinstance(excludes, list):
        return []
    if any(
        isinstance(entry, str) and entry.strip().rstrip("/") in _DOTDIR_GUARDS
        for entry in excludes
    ):
        return []

    # _line_of is section-bounded — it resets at every ``[table]`` header — so a
    # ``[tool.ruff] exclude`` in the same file cannot misanchor this.  It does
    # return 1 when it finds nothing, though, and that is reachable: TOML
    # permits ``[tool]`` plus a dotted ``pyright.exclude = [...]``, which parses
    # but never matches a bare ``exclude =``.  Line 1 is the worst possible
    # anchor — no reader would think to write the directive at the top of the
    # file — so walk progressively coarser locations and take the first that
    # resolves to a real line.
    line = next(
        (
            candidate
            for candidate in (
                _line_of(text, "exclude", section="tool.pyright"),
                _line_of(text, "pyright.exclude", section="tool"),
                _table_header_line(text, "tool.pyright"),
                _table_header_line(text, "tool"),
            )
            if candidate is not None and candidate > 1
        ),
        1,
    )

    defaults = ", ".join(PYRIGHT_DEFAULT_EXCLUDES)
    quoted = ", ".join(repr(d) for d in PYRIGHT_DEFAULT_EXCLUDES)
    return [
        _make_finding(
            rule_id=RULE_D015,
            file=rel_pyproject,
            line=line,
            column=1,
            message=(
                f"[tool.pyright] exclude replaces pyright's built-in defaults "
                f"({defaults}) instead of adding to them, and none of the "
                "entries here restores the dot-directory protection. '**/.*' is "
                "the only reason .venv is normally left out of the analysis, so "
                "with it gone a bare 'uv run pyright' — no file arguments, which "
                "is how an agent and an ad-hoc shell invoke it — type-checks "
                "every installed package in the virtualenv and does not finish "
                "inside any sane budget. pre-commit and editors pass filenames, "
                "so they stay fast and this stays invisible until something runs "
                "the bare command. .gitignore does not help: pyright does not "
                "read it (ruff does, which is where the assumption comes from). "
                f"Fix by restating the defaults alongside this repo's own "
                f"entries — add {quoted} — and keep them whenever a new entry is "
                "added. Scoping 'include' to the source roots instead is an "
                "equally valid fix and clears this rule, but it changes what "
                "gets type-checked, so make that choice deliberately."
            ),
            suppressions=parse_toml_suppressions(text),
        )
    ]
