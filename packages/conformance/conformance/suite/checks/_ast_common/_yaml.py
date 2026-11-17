"""YAML inline-comment detection for the line-based YAML checks.

The package has no YAML parser: the checks that read workflow, compose and
``atlan.yaml`` files scan lines. This is the one place that decides where a
``#`` comment starts on such a line, so every rule agrees on it.

The rules followed are YAML's own:

* ``#`` starts a comment only at the start of the line or after a space or tab.
  ``uses: org/repo@ref#frag`` keeps ``#frag``.
* ``#`` inside a quoted scalar is text. ``key: "a # b"`` has no comment.
* A quote opens a quoted scalar only where a scalar can start: at the start of
  the line, after whitespace, or after a flow indicator (``[``, ``{``, ``,``).
  The apostrophe in ``name: Bob's app # note`` is plain text, so the comment
  is still found.
* Inside double quotes a backslash escapes the next character. Inside single
  quotes ``''`` is an escaped quote.
"""

from __future__ import annotations

_QUOTE_OPENERS = frozenset(" \t[{,")


def yaml_comment_start(line: str) -> int:
    """Return the index where a YAML ``#`` comment begins on *line*.

    Returns ``len(line)`` when *line* has no comment.
    """
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == "'":
            if ch == "'":
                if line[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = None
        elif quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = None
        elif ch in ("'", '"'):
            if i == 0 or line[i - 1] in _QUOTE_OPENERS:
                quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return i
        i += 1
    return len(line)


def strip_yaml_comment(value: str) -> str:
    """Return *value* without its trailing YAML comment, right-stripped.

    ``foo # note`` gives ``foo``; ``"a # b"`` is returned whole.
    """
    return value[: yaml_comment_start(value)].rstrip()
