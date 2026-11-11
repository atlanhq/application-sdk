"""``[tool.coverage.report] exclude_lines`` hides only the lines it names.

coverage.py runs each pattern with ``re.search`` over the whole source file
(``re.MULTILINE``), and an excluded line that opens a block excludes the whole
block. An unanchored ``"pass"`` therefore matched ``passthrough_modules`` in
``create_worker``'s signature and ``imports_passed_through()`` in every
sandbox-import block, and dropped their statements from the 85% gate
(FND-4677). These tests run the configured patterns through coverage.py's own
parser, so they fail the way the gate would.
"""

from __future__ import annotations

import textwrap
import tomllib
from pathlib import Path

import pytest
from coverage.misc import join_regex
from coverage.parser import PythonParser

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: The one pattern allowed to match mid-line: it is a trailing comment.
_PRAGMA = "pragma: no cover"

#: The prefix every other pattern starts with. ``[ \t]`` rather than ``\s``:
#: under ``re.MULTILINE`` ``\s`` also matches a newline.
_LINE_START = "^[ \\t]*"


def _exclude_lines() -> list[str]:
    with _PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["tool"]["coverage"]["report"]["exclude_lines"]


def _excluded_statements(source: str) -> set[int]:
    """Line numbers of the statements in *source* the configured patterns exclude."""
    parser = PythonParser(
        text=textwrap.dedent(source), exclude=join_regex(_exclude_lines())
    )
    parser.parse_source()
    return parser.raw_statements & parser.excluded


def test_every_pattern_but_the_pragma_is_anchored_to_line_start() -> None:
    unanchored = [
        pattern
        for pattern in _exclude_lines()
        if pattern != _PRAGMA and not pattern.startswith(_LINE_START)
    ]
    assert not unanchored, (
        f"exclude_lines patterns not anchored with {_LINE_START!r}: {unanchored}. "
        "coverage.py matches anywhere in a line, so an unanchored pattern "
        "excludes any statement whose text merely contains it."
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            """\
            def create_worker(
                app,
                passthrough_modules=None,
            ):
                worker = app.build()
                return worker
            """,
            id="parameter-name-containing-pass",
        ),
        pytest.param(
            """\
            with workflow.unsafe.imports_passed_through():
                import heavy_module
                value = heavy_module.load()
            """,
            id="imports-passed-through-block",
        ),
        pytest.param(
            """\
            password = read_secret()
            bypass = compass or password
            """,
            id="identifiers-containing-pass",
        ),
        pytest.param(
            """\
            if ready:  # pass the value on
                send(value)
            """,
            id="comment-containing-pass",
        ),
        pytest.param(
            """\
            if ready:
                pass_count = 1
            """,
            id="statement-starting-with-pass",
        ),
        pytest.param(
            """\
            if ready:
                result = call(...)
            """,
            id="ellipsis-argument",
        ),
        pytest.param(
            """\
            message = "raise NotImplementedError for some backends"
            other = "def __repr__(self)"
            """,
            id="pattern-text-inside-a-string",
        ),
    ],
)
def test_a_line_with_other_tokens_is_not_excluded(source: str) -> None:
    assert _excluded_statements(source) == set()


def test_a_bare_pass_or_ellipsis_body_is_excluded() -> None:
    source = """\
        class Reader(Protocol):
            def read(self) -> bytes:
                ...

        def noop():
            pass
        """
    # Only the placeholder bodies, not the ``class``/``def`` lines above them.
    assert _excluded_statements(source) == {3, 6}
