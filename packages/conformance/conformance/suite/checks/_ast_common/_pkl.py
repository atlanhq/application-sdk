"""Pkl contract source helpers shared by the contract-reading checks (K029–K033).

The package has no Pkl parser: these checks read ``contract/**/*.pkl`` source
line by line. :func:`pkl_code_lines` blanks comments and string literals so a
pattern matches only code; :func:`contract_files` lists the hand-written
contract sources; :func:`pkl_entries` lists every mapping entry with the
blocks it sits in, and :func:`pkl_mapping_keys` the keys of a named block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Text before a ``{`` that names the block: ``env`` or ``name = new X<..>``.
# A block an entry opens is named by _ENTRY_BLOCK_RE; any other is anonymous.
_BLOCK_NAME_RE = re.compile(r"^\s*(\w+)\s*$|(\w+)\s*=\s*new\s+[\w.]+(?:<[^{}]*>)?\s*$")
# A mapping entry in masked code: the string key is blanked, the brackets stay.
_MASKED_ENTRY_RE = re.compile(r"\[\s*\]\s*(?:=|\{)")
_RAW_KEY_RE = re.compile(r'\[\s*"([^"\\]*)"\s*\]')
# Masked text before a ``{`` that an entry opens: ``["k"]`` or ``["k"] = new X``.
_ENTRY_BLOCK_RE = re.compile(r"\[\s*\]\s*(?:=\s*new\s+[\w.]+(?:<[^{}]*>)?)?\s*$")


def _string_end(text: str, i: int) -> int | None:
    r'''End index of the Pkl string literal starting at *i*, or ``None`` if none does.

    Handles ``"..."``, ``"""..."""`` and their ``#``-delimited variants. An
    unterminated single-line string stops at the line end.
    '''
    n = len(text)
    pounds = 0
    while i + pounds < n and text[i + pounds] == "#":
        pounds += 1
    if i + pounds >= n or text[i + pounds] != '"':
        return None
    j = i + pounds
    quote = '"""' if text.startswith('"""', j) else '"'
    close = quote + "#" * pounds
    j += len(quote)
    while j < n:
        # Only a string without pound delimiters treats ``\`` as an escape.
        if pounds == 0 and text[j] == "\\":
            j += 2
            continue
        if text.startswith(close, j):
            return j + len(close)
        if quote == '"' and text[j] == "\n":
            return j  # unterminated single-line string: stop at the line end
        j += 1
    return j


def _literal_end(text: str, i: int) -> int | None:
    """End index of the comment or string literal starting at *i*, else ``None``."""
    if text.startswith("//", i):
        end = text.find("\n", i)
        return len(text) if end == -1 else end
    if text.startswith("/*", i):
        end = text.find("*/", i + 2)
        return len(text) if end == -1 else end + 2
    return _string_end(text, i)


def pkl_code_lines(text: str) -> list[str]:
    r'''``text``'s lines with Pkl comments and string literals blanked out.

    Covers ``//`` and ``/* */`` comments and every Pkl string form: ``"..."``,
    ``"""..."""`` and their custom-delimiter variants ``#"..."#`` and
    ``#"""..."""#``. Blanked characters become spaces (newlines are kept), so
    line and column numbers still match the source. A ``\(...)`` interpolation
    is blanked with the string it sits in.
    '''
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        end = _literal_end(text, i)
        if end is None:
            out.append(text[i])
            i += 1
            continue
        out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
        i = end
    return "".join(out).splitlines()


def contract_files(root: Path) -> list[Path]:
    """Hand-written ``contract/**/*.pkl``, skipping hidden dirs (pkl caches)."""
    contract_dir = root / "contract"
    return sorted(
        p
        for p in contract_dir.rglob("*.pkl")
        if not any(part.startswith(".") for part in p.relative_to(contract_dir).parts)
    )


@dataclass(frozen=True)
class PklEntry:
    """One ``["key"] = …`` / ``["key"] { … }`` mapping entry of a contract."""

    path: tuple[str | None, ...]
    """Names of the enclosing blocks, outermost first. A named block is its
    name (``deploy``, ``pools``, ``env``); a block opened by a mapping entry is
    ``[key]``; any other block is ``None``."""
    key: str
    line: int
    """1-based line of the entry."""
    column: int
    """0-based column of the entry's ``[``."""


def _block_name(code: str, raw: str, seg: int, end: int) -> str | None:
    """The name of the block whose ``{`` is at *end*, from the text since *seg*."""
    m = _BLOCK_NAME_RE.search(code[seg:end])
    if m is not None:
        return m.group(1) or m.group(2)
    entry = _ENTRY_BLOCK_RE.search(code[seg:end])
    if entry is None:
        return None
    key = _RAW_KEY_RE.match(raw, seg + entry.start())
    return f"[{key.group(1)}]" if key is not None else None


def pkl_entries(text: str) -> list[PklEntry]:
    """Every mapping entry of *text* with the path of blocks it sits in.

    Keys in comments and strings are ignored; a key containing a backslash (an
    escape or an interpolation) is skipped rather than guessed at.
    """
    stack: list[str | None] = []
    raw_lines = text.splitlines()
    entries: list[PklEntry] = []
    for lineno, code in enumerate(pkl_code_lines(text), start=1):
        raw_line = raw_lines[lineno - 1]
        seg = 0  # start of the text since the last brace on this line
        for i, ch in enumerate(code):
            if ch == "{":
                stack.append(_block_name(code, raw_line, seg, i))
                seg = i + 1
            elif ch == "}":
                if stack:
                    stack.pop()
                seg = i + 1
            elif ch == "[" and _MASKED_ENTRY_RE.match(code, i) is not None:
                raw = _RAW_KEY_RE.match(raw_line, i)
                if raw is not None:
                    entries.append(PklEntry(tuple(stack), raw.group(1), lineno, i))
    return entries


def pkl_mapping_keys(text: str, block: str, *, within: str) -> list[tuple[str, int]]:
    """``(key, line)`` of each entry directly inside a block named *block*.

    The block (``pools { … }`` or ``pools = new Mapping<…> { … }``) must itself
    be nested somewhere inside a block named *within*. Source order.
    """
    return [
        (e.key, e.line)
        for e in pkl_entries(text)
        if e.path and e.path[-1] == block and within in e.path[:-1]
    ]
