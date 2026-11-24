"""Pkl contract source helpers shared by the contract-reading checks (K029–K031).

The package has no Pkl parser: these checks read ``contract/**/*.pkl`` source
line by line. :func:`pkl_code_lines` blanks comments and string literals so a
pattern matches only code; :func:`contract_files` lists the hand-written
contract sources.
"""

from __future__ import annotations

from pathlib import Path


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
