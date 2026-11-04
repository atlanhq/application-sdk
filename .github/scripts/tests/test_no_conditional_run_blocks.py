"""No conditional logic in an inlined ``run:`` block (docs/standards/ci.md).

The standard says a ``run:`` block holds straight-line commands only: the
moment a step branches (``if``/``case``/a loop, or ``[ … ] && …``), the logic
moves into a tested script under ``.github/scripts/``. Until this guard,
nothing enforced it, and composite actions are not linted by actionlint at all.

This walks every workflow under ``.github/workflows/`` and every composite
``action.y*ml`` under ``.github/actions/``, and fails on a conditional ``run:``
block that is not in ``.github/scripts/conditional_run_allowlist.txt``.

The allowlist is a ratchet. It was seeded with the blocks that existed when the
guard landed, and it may only shrink:

* an entry whose block is gone or no longer branches fails as stale, so a
  migration has to delete its line;
* the file may not grow past ``ALLOWLIST_CEILING``, so adding an entry means
  editing this test as well, in plain sight of review. Lower the ceiling when
  you remove entries.

Detection is lexical, not a shell parser. Quoted strings, comments and
``${{ … }}`` expressions are blanked first so prose like ``echo "…; if it ran"``
does not count. Inline interpreters (``python3 - <<EOF``) are scanned too: a
heredoc that branches is still untested logic living in YAML.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
ALLOWLIST = REPO_ROOT / ".github/scripts/conditional_run_allowlist.txt"

#: Entries the allowlist may hold. Only ever lower this.
ALLOWLIST_CEILING = 39

_EXPR = re.compile(r"\$\{\{.*?\}\}", re.S)
_DQ = re.compile(r'"(?:\\.|[^"\\\n])*"')
_SQ = re.compile(r"'[^'\n]*'")
# A shell compound keyword in command position: line start, after a command
# separator / opening group, or after another keyword that starts a list.
_KEYWORD = re.compile(
    r"(?:^\s*|[;&|({]\s*|\b(?:then|do|else)\s+)"
    r"(?P<kw>if|for|while|until|case|select)\b(?=\s)",
    re.M,
)
# A test whose result picks the next command: `[ … ] && x`, `[[ … ]] || y`,
# `test -n "$X" || { …; }`.
_TEST_CHAIN = re.compile(
    r"(?:\]\]?|(?:^|[;&|])\s*test\s[^\n;&|]*)\s*(?P<op>&&|\|\|)",
    re.M,
)


def _blank(script: str) -> str:
    """Drop comments, expressions and quoted text, keeping the shell skeleton."""
    lines = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    text = _EXPR.sub("X", "\n".join(lines))
    text = _DQ.sub('""', text)
    return _SQ.sub("''", text)


def conditional_reason(script: str) -> str | None:
    """Why ``script`` counts as conditional, or None when it is straight-line."""
    text = _blank(script)
    if match := _KEYWORD.search(text):
        return f"`{match.group('kw')}`"
    if match := _TEST_CHAIN.search(text):
        return f"test chained with `{match.group('op')}`"
    return None


def _step_label(step: dict, index: int) -> str:
    return str(step.get("id") or step.get("name") or f"#{index}")


def _run_blocks(path: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(location, script)`` for every ``run:`` step in ``path``."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "jobs" in doc:
        groups = [
            (f"jobs.{job_id}", (job or {}).get("steps") or [])
            for job_id, job in doc["jobs"].items()
        ]
    else:
        groups = [("runs", (doc.get("runs") or {}).get("steps") or [])]
    for prefix, steps in groups:
        for index, step in enumerate(steps):
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                yield f"{prefix}.{_step_label(step, index)}", step["run"]


def _sources() -> list[Path]:
    github = REPO_ROOT / ".github"
    return sorted(
        [*github.glob("workflows/*.y*ml"), *github.glob("actions/*/action.y*ml")]
    )


def conditional_blocks() -> dict[str, str]:
    """Map ``<file>::<location>`` to its reason, for every conditional block."""
    found: dict[str, str] = {}
    for path in _sources():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for location, script in _run_blocks(path):
            reason = conditional_reason(script)
            if reason is None:
                continue
            key = f"{rel}::{location}"
            # Two steps may share a name; number the repeats so keys stay unique.
            n = 2
            while key in found:
                key = f"{rel}::{location}#{n}"
                n += 1
            found[key] = reason
    return found


def _allowlist() -> list[str]:
    return [
        ln.strip()
        for ln in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def test_no_unlisted_conditional_run_block() -> None:
    allowed = set(_allowlist())
    new = {k: v for k, v in conditional_blocks().items() if k not in allowed}
    assert not new, (
        "These run: blocks branch (docs/standards/ci.md forbids conditional "
        "shell inline). Move the logic into a tested script under "
        ".github/scripts/ and call it from the step:\n"
        + "\n".join(f"  {k}  ({v})" for k, v in sorted(new.items()))
    )


def test_allowlist_has_no_stale_entries() -> None:
    current = conditional_blocks()
    stale = [k for k in _allowlist() if k not in current]
    assert not stale, (
        "These allowlisted run: blocks no longer exist or no longer branch. "
        "Delete their lines from .github/scripts/conditional_run_allowlist.txt "
        "and lower ALLOWLIST_CEILING to match:\n" + "\n".join(f"  {k}" for k in stale)
    )


def test_allowlist_has_no_duplicates() -> None:
    entries = _allowlist()
    assert len(entries) == len(set(entries))


def test_allowlist_only_shrinks() -> None:
    entries = _allowlist()
    assert len(entries) <= ALLOWLIST_CEILING, (
        f"The allowlist holds {len(entries)} entries, above the ceiling of "
        f"{ALLOWLIST_CEILING}. New conditional run: blocks are not allowed; "
        "put the logic in a script instead."
    )


@pytest.mark.parametrize(
    "script",
    [
        'if [ -n "$X" ]; then echo y; fi',
        "  if true; then\n    :\n  fi",
        "for f in *.txt; do echo $f; done",
        "while read -r line; do :; done < f",
        'case "$X" in a) ;; esac',
        "set -e; until curl -sf x; do sleep 1; done",
        "[ -f x ] && echo present",
        '[[ "$X" == y ]] || exit 1',
        'test -n "$REF" || {\n  echo missing\n  exit 1\n}',
        "python3 - <<EOF\nif x:\n    pass\nEOF",
    ],
)
def test_branching_scripts_are_flagged(script: str) -> None:
    assert conditional_reason(script) is not None


@pytest.mark.parametrize(
    "script",
    [
        "uv sync --all-extras\nuv run pytest",
        'echo "::error::if it ran, check its logs; for details see x"',
        "echo 'while you wait, do nothing'",
        "# if this were code it would branch\necho hi",
        "echo \"${{ inputs.a || 'b' }}\"",
        'echo "ref=${RUNTIME:-$SHARED}" >> "$GITHUB_OUTPUT"',
        "chmod +x /tmp/x.sh || true",
        "make build && make test",
        "python3 .github/scripts/do_the_thing.py --verify-if-present",
    ],
)
def test_straight_line_scripts_pass(script: str) -> None:
    assert conditional_reason(script) is None
