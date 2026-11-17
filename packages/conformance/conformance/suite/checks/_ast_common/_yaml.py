"""YAML inline-comment detection for the line-based YAML checks.

The package has no YAML parser: the checks that read workflow, compose and
``atlan.yaml`` files scan lines. This is the one place that decides where a
``#`` comment starts on such a line, so every rule agrees on it.

The rules followed are YAML's own:

* ``#`` starts a comment only at the start of the line or after a space or tab.
  ``uses: org/repo@ref#frag`` keeps ``#frag``.
* ``#`` inside a quoted scalar is text. ``key: "a # b"`` has no comment.
* A quote opens a quoted scalar only where a scalar can start: at the start of
  the text, after a ``key:`` or ``- `` indicator, after a flow indicator
  (``[``, ``{``, ``,``), or after an anchor or tag. A quote inside a plain
  scalar is plain text: in ``name: Bob's app # note`` and
  ``run: echo "step # 1"`` the ``#`` starts a comment.
* In a flow collection a ``:`` right after a quoted key is an indicator, so in
  ``{"key":"a # b"}`` the value is quoted.
* Inside double quotes a backslash escapes the next character. Inside single
  quotes ``''`` is an escaped quote.
"""

from __future__ import annotations

_BLANK = frozenset(" \t")
_ENDS_TOKEN = frozenset(("", " ", "\t"))
_FLOW_INDICATORS = frozenset(",[]{}")


def yaml_comment_start(line: str) -> int:
    """Return the index where a YAML ``#`` comment begins on *line*.

    *line* is a whole line or the text after a ``key:``; a scalar may start at
    its first non-blank character. Returns ``len(line)`` when *line* has no
    comment.
    """
    quote: str | None = None
    flow_depth = 0
    can_start = True  # a scalar may begin at the next non-blank character
    after_quoted = False  # the last token was a closed quoted scalar
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        nxt = line[i + 1 : i + 2]
        if quote == "'":
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                quote = None
                after_quoted = True
            i += 1
            continue
        if quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = None
                after_quoted = True
            i += 1
            continue

        if ch in _BLANK:
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1] in _BLANK):
            return i

        closed, after_quoted = after_quoted, False
        if ch in ("'", '"') and can_start:
            quote = ch
            can_start = False
        elif ch in "[{" and (can_start or flow_depth):
            flow_depth += 1
            can_start = True
        elif ch in "]}" and flow_depth:
            flow_depth -= 1
            can_start = False
        elif ch == "," and flow_depth:
            can_start = True
        elif ch == ":" and (
            nxt in _ENDS_TOKEN or closed or (flow_depth and nxt in _FLOW_INDICATORS)
        ):
            can_start = True
        elif ch in "-?" and can_start and nxt in _ENDS_TOKEN:
            pass  # sequence entry or complex key; a scalar may still follow
        elif ch in "&!" and can_start:
            while i < n and line[i] not in _BLANK:
                i += 1
            continue  # anchor or tag; a scalar may still follow
        else:
            can_start = False
        i += 1
    return n


def strip_yaml_comment(value: str) -> str:
    """Return *value* without its trailing YAML comment, right-stripped.

    ``foo # note`` gives ``foo``; ``"a # b"`` is returned whole.
    """
    return value[: yaml_comment_start(value)].rstrip()
