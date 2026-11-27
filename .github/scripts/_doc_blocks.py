"""Fenced code blocks and `legacy-example` markers in Markdown, read one way.

Two docs gates read the same pages: check_docs_deprecated_imports.py (no
deprecated SDK symbols, FND-4268) and check_doc_examples.py (blocks parse and
their SDK imports resolve, FND-4272). Both take their blocks and markers from
here, so the two cannot disagree about where a block starts or ends, which
blocks a marker covers, or what counts as a marker. Each check passes its own
set of fence tags; the validation rules stay in each check. Stdlib only.

A block that shows a deprecated or v2 API on purpose carries a marker, with a
reason, on the line directly above its opening fence:

    <!-- legacy-example: v2 "before" snippet -->
    ```python
    from application_sdk.handler import HandlerError
    ```
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

#: The marker, shown in failure messages.
LEGACY_MARKER = "<!-- legacy-example: <reason> -->"

_FENCE_OPEN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})\s*\{?\.?(?P<tag>[\w+-]*)"
)
_MARKER = re.compile(r"^\s*<!--\s*legacy-example:\s*(?P<reason>\S.*?)\s*-->\s*$")
_BARE_MARKER = re.compile(r"^\s*<!--\s*legacy-example\b")


@dataclass(frozen=True)
class CodeBlock:
    """One fenced code block: its body and the file line of its first body line."""

    first_line: int
    body: str
    marked: bool


def _dedent(line: str, indent: str) -> str:
    return line[len(indent) :] if line.startswith(indent) else line.lstrip()


def iter_code_blocks(text: str, tags: frozenset[str]) -> Iterator[CodeBlock]:
    """Yield every fenced block in *text* whose tag (lower-cased) is in *tags*.

    An untagged fence has the tag ``""``. Fences may be indented (inside
    admonitions and tabs); a block's lines are dedented by its opening fence's
    indent. ``marked`` is set when the line directly above the opening fence is
    a `legacy-example` marker with a reason.
    """
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        opened = _FENCE_OPEN.match(lines[index])
        if opened is None:
            index += 1
            continue
        indent, fence, tag = opened["indent"], opened["fence"], opened["tag"]
        # CommonMark: the closing fence is the opener's character, at least as
        # long, with nothing but whitespace after it. A ```python line inside
        # a string in the example does not close it.
        closing = re.compile(rf"[ \t]*{re.escape(fence[0])}{{{len(fence)},}}[ \t]*")
        end = index + 1
        while end < len(lines) and not closing.fullmatch(lines[end]):
            end += 1
        if tag.lower() in tags:
            above = lines[index - 1] if index > 0 else ""
            yield CodeBlock(
                first_line=index + 2,
                body="\n".join(
                    _dedent(line, indent) for line in lines[index + 1 : end]
                ),
                marked=_MARKER.match(above) is not None,
            )
        index = end + 1


def bare_markers(text: str) -> list[int]:
    """Line numbers of `legacy-example` markers that give no reason."""
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if _BARE_MARKER.match(line) and not _MARKER.match(line)
    ]
