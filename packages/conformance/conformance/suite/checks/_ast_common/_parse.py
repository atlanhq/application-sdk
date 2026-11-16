"""The parse-failure record: a file no check could scan.

Every AST-based check catches ``SyntaxError`` and moves on, because one bad
file must not abort the run.  Taken alone, that skip is silent: a file the
runner's Python cannot parse produces no findings, and no findings reads as a
clean file.  Most checks parse files independently (43 of the 49
``except SyntaxError`` sites return empty), so fixing it in each check would
mean 43 edits, and every new check would need the same fix.

Instead the runner parses each discovered Python file once with
:func:`parse_failure` and reports every failure as a SARIF
``toolExecutionNotification``.  The per-check skips stay as they are: the file
is still skipped, but the report now says so.
"""

from __future__ import annotations

import ast
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ParseFailure", "parse_failure", "runtime_python"]


def runtime_python() -> str:
    """``"3.12"``-style version of the interpreter doing the parsing."""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


@dataclass(frozen=True)
class ParseFailure:
    """A Python file that the runner's interpreter could not parse.

    ``file`` is repo-root-relative and POSIX-style, the same shape as
    :attr:`Finding.file`.  ``line`` and ``column`` are 1-based, and both are
    at least 1 even when the parser reports no position.
    """

    file: str
    line: int
    column: int
    reason: str
    python: str

    @property
    def message(self) -> str:
        return (
            f"{self.file} could not be parsed by Python {self.python} "
            f"({self.reason}, line {self.line}), so no check scanned it. Fix the syntax error, "
            f"or run the suite on the Python version the app targets."
        )


def parse_failure(path: Path, root: Path) -> ParseFailure | None:
    """Parse *path* and return a :class:`ParseFailure` if it does not parse.

    Reads bytes so the file's own ``# -*- coding: ... -*-`` declaration is
    honoured, exactly as the interpreter would import it.  A file that cannot
    be read at all (``OSError``) returns ``None``: it is not a parse failure,
    and the read-failure path belongs to :mod:`._io`.
    """
    try:
        source = path.read_bytes()
    except OSError:
        return None
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.as_posix()
    try:
        # A SyntaxWarning (e.g. an invalid "\s" escape) is not a parse failure,
        # but under ``-W error`` it would raise as one, and otherwise it spams
        # the CI log with warnings about the app's code.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return ParseFailure(
            file=rel,
            line=max(exc.lineno or 1, 1),
            column=max(exc.offset or 1, 1),
            reason=exc.msg,
            python=runtime_python(),
        )
    except ValueError as exc:
        # Python < 3.12 raises ValueError for a NUL byte in the source.
        return ParseFailure(
            file=rel, line=1, column=1, reason=str(exc), python=runtime_python()
        )
    return None
