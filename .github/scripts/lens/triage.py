"""Separate mechanical change from real change — in code, before any model call.

A PR that touches 100 files is often a rename, a reformat, an import move or
a docstring sweep. None of that needs a model, and paying one to read it is
both slow and the fastest way to exhaust a PR's budget on nothing. Each rule
here is a proof, not a heuristic guess:

- **AST-identical** (Python): old and new parse to the same tree once
  docstrings are dropped — formatting, comments, quoting, docstrings. The
  interpreter cannot tell the two versions apart, so there is nothing for a
  reviewer to find.
- **Whitespace-only** hunk (any file): the changed lines are equal once
  whitespace is normalised. In Python leading indentation is syntax, so it
  is kept: a re-indented statement is a real change.
- **Import-only** hunk (Python): every changed line is an import statement
  and nothing else, and one is added. Unused or missing imports are
  ruff/pyright territory, not judgment. A pure deletion is never mechanical
  here: removing only imports can drop a re-export or a side-effect import.
- **Mechanical rename**: every changed line differs from its old version
  only by token swaps of names drawn from one small set that recurs across
  files (`get_x → fetch_x` in 35 files). Reviewed as one line, not 35 files.
  A keyword or a literal (`False → True`, `"safe" → "unsafe"`) is never a
  rename.
- **Duplicate hunk**: the identical hunk (header, context and changed lines)
  in several files is reviewed once, in the first file; the rest are listed
  as "same change".

What survives is what the model reviews. What does not is reported in the
summary with its reason, so nothing is skipped silently.
"""

from __future__ import annotations

import ast
import hashlib
import keyword
import re
from collections import Counter
from dataclasses import dataclass, field

from .diff import FileDiff, Hunk

# A quoted string is one token: a word inside it is a value (`"safe"` ->
# `"unsafe"`), and a swap of a whole literal is never a rename.
_TOKEN = re.compile(
    r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[A-Za-z_][A-Za-z0-9_]*|\d+|\S'
)
_IMPORT = re.compile(r"^\s*(import\s+\S|from\s+\S+\s+import\s)")
_IMPORT_OPEN = re.compile(r"from\s+\S+\s+import\s*\(\s*(#.*)?")
# Files whose leading indentation is syntax.
_INDENTED = (".py", ".pyi")
# Literal words of Python, JSON, YAML and TOML: a value, never a name to rename.
_LITERAL_WORDS = frozenset(
    {"true", "false", "none", "null", "nil", "yes", "no", "on", "off"}
)


@dataclass
class Triage:
    reviewed: list[FileDiff] = field(default_factory=list)  # substantive part only
    mechanical: dict[str, list[str]] = field(default_factory=dict)  # reason -> paths
    renames: list[tuple[str, str]] = field(
        default_factory=list
    )  # (old, new) token swaps
    duplicate_of: dict[str, str] = field(
        default_factory=dict
    )  # path -> representative path
    hunks_dropped: int = 0

    def note(self, reason: str, path: str) -> None:
        paths = self.mechanical.setdefault(reason, [])
        if path not in paths:
            paths.append(path)

    def summary_lines(self) -> list[str]:
        out = []
        for reason, paths in self.mechanical.items():
            out.append(f"{reason}: {len(paths)} file(s)")
        if self.duplicate_of:
            out.append(
                f"same change repeated in other files (reviewed once): {len(self.duplicate_of)} file(s)"
            )
        return out


# ---- proofs ------------------------------------------------------------------------


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


def ast_identical(old: str, new: str) -> bool:
    """True only when both parse and are the same program modulo docstrings."""
    try:
        a = _strip_docstrings(ast.parse(old))
        b = _strip_docstrings(ast.parse(new))
    except (SyntaxError, ValueError):
        return False
    return ast.dump(a, include_attributes=False) == ast.dump(
        b, include_attributes=False
    )


def _norm(s: str) -> str:
    return " ".join(s.split())


def _norm_indented(s: str) -> str:
    """`s` normalised like `_norm`, its leading indentation kept: in Python the
    indentation is syntax, and a dedent moves a statement out of its block."""
    return s[: len(s) - len(s.lstrip())] + _norm(s)


def _sides(h: Hunk) -> tuple[list[str], list[str]]:
    return [ln.text for ln in h.lines if ln.kind == "-"], [
        ln.text for ln in h.lines if ln.kind == "+"
    ]


def hunk_whitespace_only(h: Hunk, *, indent_matters: bool = False) -> bool:
    """The changed lines are the same once whitespace is normalised. With
    `indent_matters` (Python), a change of leading indentation is not
    whitespace: `send()` dedented out of an `if` block is a change of control flow."""
    norm = _norm_indented if indent_matters else _norm
    rem, add = _sides(h)
    return [norm(x) for x in rem if x.strip()] == [norm(x) for x in add if x.strip()]


def _import_line(line: str) -> bool:
    """A line that is import statements and nothing else (`import os; run()`
    is not), or the opening line of a parenthesised `from m import (`."""
    if not _IMPORT.match(line):
        return False
    text = line.strip()
    try:
        body = ast.parse(text).body
    except (SyntaxError, ValueError):
        return bool(_IMPORT_OPEN.fullmatch(text))
    return bool(body) and all(isinstance(n, (ast.Import, ast.ImportFrom)) for n in body)


def hunk_imports_only(h: Hunk) -> bool:
    """Every changed line is an import statement and nothing else, and at least
    one is added. A hunk that only removes imports is not proven mechanical: it
    can drop a re-export (a deleted `__init__.py`) or an import kept for its
    side effect."""
    rem, add = _sides(h)
    changed = [x for x in rem + add if x.strip()]
    return any(x.strip() for x in add) and all(_import_line(x) for x in changed)


def _renameable(token: str) -> bool:
    """A token a rename can swap: a name, not a keyword or a literal word.
    `False` -> `True`, `and` -> `or` or `enabled: false` -> `true` change what
    the code does, however many files repeat them."""
    return not keyword.iskeyword(token) and token.lower() not in _LITERAL_WORDS


def line_swaps(old: str, new: str) -> list[tuple[str, str]] | None:
    """Token swaps turning `old` into `new`, or None if it is not a pure swap."""
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    if len(a) != len(b):
        return None
    swaps = [(x, y) for x, y in zip(a, b) if x != y]
    if not swaps or not all(
        re.match(r"[A-Za-z_]", x)
        and re.match(r"[A-Za-z_]", y)
        and _renameable(x)
        and _renameable(y)
        for x, y in swaps
    ):
        return None
    return swaps


def hunk_swaps(h: Hunk) -> set[tuple[str, str]] | None:
    rem, add = _sides(h)
    rem = [x for x in rem if x.strip()]
    add = [x for x in add if x.strip()]
    if not rem or len(rem) != len(add):
        return None
    out: set[tuple[str, str]] = set()
    for o, n in zip(rem, add):
        s = line_swaps(o, n)
        if s is None:
            return None
        out.update(s)
    return out


def _hunk_signature(h: Hunk, *, indent_matters: bool = False) -> str:
    """The whole hunk: its header, context and changed lines, in order. The
    same edit under different context (`timeout: 30` -> `60` configuring two
    different components) is a different change and is reviewed in each file;
    only a hunk identical down to its context is the same change."""
    norm = _norm_indented if indent_matters else _norm
    key = (
        _norm(h.header)
        + "\x01"
        + "\x00".join(f"{ln.kind}{norm(ln.text)}" for ln in h.lines)
    )
    return hashlib.sha1(key.encode()).hexdigest()


def _with_hunks(fd: FileDiff, hunks: list[Hunk]) -> FileDiff:
    return FileDiff(
        path=fd.path,
        old_path=fd.old_path,
        status=fd.status,
        is_binary=fd.is_binary,
        hunks=hunks,
    )


# ---- the pass ----------------------------------------------------------------------


def triage(
    files: list[FileDiff],
    old_text: dict[str, str | None],
    new_text: dict[str, str | None],
    *,
    rename_min_files: int = 3,
    rename_max_swaps: int = 4,
    duplicate_min_files: int = 3,
) -> Triage:
    """Split `files` into the substantive diff to review and the mechanical rest.

    `old_text`/`new_text` are whole-file contents at the two ends of the
    reviewed range (None when unavailable — then the AST proof is skipped,
    never assumed)."""
    t = Triage()

    # Pass 1: which token swaps recur across files? A swap set is a rename only
    # when it is small and shows up in several files; one-off swaps are edits.
    swap_files: Counter[tuple[str, str]] = Counter()
    for fd in files:
        seen: set[tuple[str, str]] = set()
        for h in fd.hunks:
            s = hunk_swaps(h)
            if s:
                seen.update(s)
        swap_files.update(seen)
    rename_set = {s for s, n in swap_files.items() if n >= rename_min_files}
    if len(rename_set) > rename_max_swaps:
        rename_set = set(
            sorted(rename_set, key=lambda s: -swap_files[s])[:rename_max_swaps]
        )
    t.renames = sorted(rename_set)

    # Pass 2: duplicate hunk signatures across files.
    sig_files: dict[str, list[str]] = {}
    for fd in files:
        py = fd.path.endswith(_INDENTED)
        for h in fd.hunks:
            owners = sig_files.setdefault(_hunk_signature(h, indent_matters=py), [])
            if fd.path not in owners:
                owners.append(fd.path)

    for fd in files:
        py = fd.path.endswith(_INDENTED)
        if fd.path.endswith(".py") and fd.status == "modified":
            old, new = old_text.get(fd.path), new_text.get(fd.path)
            if old is not None and new is not None and ast_identical(old, new):
                t.note(
                    "behaviour-identical (formatting, comments or docstrings; AST unchanged)",
                    fd.path,
                )
                t.hunks_dropped += len(fd.hunks)
                continue
        keep: list[Hunk] = []
        dup_rep: str | None = None
        for h in fd.hunks:
            if hunk_whitespace_only(h, indent_matters=py):
                t.note("whitespace-only", fd.path)
            elif fd.path.endswith(".py") and hunk_imports_only(h):
                t.note("imports only", fd.path)
            elif (s := hunk_swaps(h)) is not None and s <= rename_set:
                t.note("mechanical rename", fd.path)
            else:
                owners = sig_files.get(_hunk_signature(h, indent_matters=py), [])
                if len(owners) >= duplicate_min_files and owners[0] != fd.path:
                    dup_rep = owners[0]
                else:
                    keep.append(h)
                    continue
            t.hunks_dropped += 1
        if keep:
            t.reviewed.append(
                fd if len(keep) == len(fd.hunks) else _with_hunks(fd, keep)
            )
        elif dup_rep:
            t.duplicate_of[fd.path] = dup_rep
    return t
