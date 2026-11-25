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
  Nor is a hunk that swaps the module imported (`import json` -> `import
  orjson`): that changes which code runs, unless the two modules are one
  file this pull request renames.
- **Mechanical rename**: every changed line differs from its old version
  only by token swaps of names drawn from one small set that recurs across
  files (`get_x → fetch_x` in 35 files). Reviewed as one line, not 35 files.
  A keyword or a literal (`False → True`, `"safe" → "unsafe"`) is never a
  rename. Both names must be defined in this repository: the old one in a
  base copy of a changed file, the new one in a head copy (a function,
  class, variable or parameter; never a name bound by an import), or be
  modules of its changed paths. A name after a dot must be a member defined
  here (a method, class attribute, `self.x`, or a module-level def): a swap
  of a library's names (`json` -> `orjson`, `logger.error` ->
  `logger.warning`, `client.get` -> `client.post`) changes behaviour and is
  line-reviewed, however many files repeat it (FND-5316).
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
from collections.abc import Mapping
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

    def withheld(self) -> dict[str, list[str]]:
        """`mechanical` narrowed to the files the model never saw: reason ->
        paths with no hunk left to review. A reason is noted per hunk, so a
        file whose import hunk was set aside and whose real change was
        reviewed is in `mechanical` and not here (FND-5316)."""
        sent = {fd.path for fd in self.reviewed}
        out: dict[str, list[str]] = {}
        for reason, paths in self.mechanical.items():
            kept = [p for p in paths if p not in sent]
            if kept:
                out[reason] = kept
        return out

    def summary_lines(self) -> list[str]:
        """What was not sent to the model, a line per reason. Only withheld
        files count: these lines say "not line-reviewed"."""
        out: list[str] = []
        for reason, paths in self.withheld().items():
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


def _imported_bindings(line: str) -> set[tuple[str, str, str]]:
    """What an import line binds, as (module, name, alias): `import a.b as c`
    -> (a.b, "", c); `from .m import x as y` -> (.m, x, y); the opening line
    `from m import (` -> (m, "(", "")."""
    text = line.strip()
    try:
        body = ast.parse(text).body
    except (SyntaxError, ValueError):
        m = _IMPORT_OPEN.fullmatch(text)
        return {(text.split()[1], "(", "")} if m else set()
    out: set[tuple[str, str, str]] = set()
    for n in body:
        if isinstance(n, ast.Import):
            out.update((a.name, "", a.asname or "") for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            module = "." * n.level + (n.module or "")
            out.update((module, a.name, a.asname or "") for a in n.names)
    return out


def hunk_imports_only(h: Hunk, renamed_modules: dict[str, str] | None = None) -> bool:
    """Every changed line is an import statement and nothing else, at least
    one is added, and nothing imported is swapped for something else. A hunk
    that only removes imports is not proven mechanical: it can drop a
    re-export (a deleted `__init__.py`) or an import kept for its side effect.
    A hunk that drops one import and adds another (`import json` -> `import
    orjson`, `from a import f` -> `from b import f`, `from m import f as send`
    -> `from m import g as send`) changes which code runs (FND-5316), unless
    the only difference is a module this pull request renames
    (`renamed_modules`: old dotted name -> new). Imports added, regrouped or
    reordered are still mechanical."""
    rem, add = _sides(h)
    changed = [x for x in rem + add if x.strip()]
    if not (any(x.strip() for x in add) and all(_import_line(x) for x in changed)):
        return False
    old = set().union(*(_imported_bindings(x) for x in rem if x.strip()))
    new = set().union(*(_imported_bindings(x) for x in add if x.strip()))
    gone, came = old - new, new - old
    if not gone:
        return True  # imports added, regrouped or reordered: nothing swapped
    moves = renamed_modules or {}
    moved = {(moves[m], n, a) for m, n, a in gone if m in moves}
    return len(moved) == len(gone) and moved == came


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


def hunk_swaps(
    h: Hunk, defined: Definitions | None = None
) -> set[tuple[str, str]] | None:
    """The token swaps of a hunk whose changed lines are pure swaps, or None.
    With `defined`, every swap must be a rename of names this repository
    defines (`Definitions.renames`); one that is not makes the hunk None."""
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
        if defined is not None and not all(
            defined.renames(x, y, receiver=r) for x, y, r in _swap_positions(o, n)
        ):
            return None
        out.update(s)
    return out


def _swap_positions(old: str, new: str) -> list[tuple[str, str, str | None]]:
    """Each swap of a pure-swap line pair, with its receiver when it sits
    after a dot (`logger` for `logger.error`; "" when the dot follows no
    name, such as `f().error`), or None when it is a plain name."""
    a, b = _TOKEN.findall(old), _TOKEN.findall(new)
    out: list[tuple[str, str, str | None]] = []
    for i, (x, y) in enumerate(zip(a, b)):
        if x == y:
            continue
        side = (
            a if i > 0 and a[i - 1] == "." else b if i > 0 and b[i - 1] == "." else None
        )
        if side is None:
            out.append((x, y, None))
        else:
            before = side[i - 2] if i > 1 else ""
            out.append((x, y, before if re.match(r"[A-Za-z_]\w*$", before) else ""))
    return out


@dataclass
class Definitions:
    """The names this repository defines, read from the changed files' base
    copies (`old`) and head copies (`new`): what a mechanical rename may swap.

    `names`: functions, classes, variables, parameters and exception names,
    and the module names of the changed paths. `members`: what may follow a
    dot: methods and attributes of a class defined here, `self.x`/`cls.x`
    assignments, and module-level defs and classes (`module.f`). `receivers`:
    what such a member may follow, the changed paths' modules and the classes
    defined here (`self` and `cls` always). A name bound only by an import is
    in none of them: it names code outside the repository."""

    old_names: set[str] = field(default_factory=set)
    new_names: set[str] = field(default_factory=set)
    old_members: set[str] = field(default_factory=set)
    new_members: set[str] = field(default_factory=set)
    receivers: set[str] = field(default_factory=lambda: {"self", "cls"})

    def renames(self, old: str, new: str, *, receiver: str | None = None) -> bool:
        """Whether `old -> new` is a rename of names defined here: the old
        name on the base side, the new one on the head side. After a dot
        (`receiver` not None), the receiver must be this repository's too
        (`self`, a class or a module defined here): `logger.error` ->
        `logger.warning` is never a rename, even when some changed file
        defines an `error` and a `warning` of its own."""
        if receiver is not None:
            return (
                receiver in self.receivers
                and old in self.old_members
                and new in self.new_members
            )
        return old in self.old_names and new in self.new_names


def _module_names(path: str) -> set[str]:
    """`app/a/b.py` -> {b, app.a.b}; `app/a/__init__.py` -> {a, app.a}."""
    if not path.endswith((".py", ".pyi")):
        return set()
    parts = path.rsplit(".", 1)[0].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return {parts[-1], ".".join(parts)} if parts else set()


def _defined_in(text: str) -> tuple[set[str], set[str], set[str]]:
    """(names, members, classes) one Python file defines; empty when it does
    not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set(), set(), set()
    names: set[str] = set()
    members: set[str] = set()
    classes: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            members.add(node.name)  # reached as `module.name`
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
                if arg is not None:
                    names.add(arg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
                members.add(node.attr)
        if isinstance(node, ast.ClassDef):
            classes.add(node.name)
            for stmt in node.body:
                if isinstance(
                    stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    members.add(stmt.name)
                targets = (
                    stmt.targets
                    if isinstance(stmt, ast.Assign)
                    else [stmt.target]
                    if isinstance(stmt, ast.AnnAssign)
                    else []
                )
                members.update(t.id for t in targets if isinstance(t, ast.Name))
    return names, members, classes


def definitions(
    files: list[FileDiff],
    old_text: Mapping[str, str | None],
    new_text: Mapping[str, str | None],
) -> Definitions:
    """What the changed files define on each side (see `Definitions`)."""
    d = Definitions()
    for fd in files:
        d.old_names |= _module_names(fd.old_path or fd.path)
        d.new_names |= _module_names(fd.path)
        d.old_members |= _module_names(fd.old_path or fd.path)
        d.new_members |= _module_names(fd.path)
        d.receivers |= _module_names(fd.old_path or fd.path) | _module_names(fd.path)
    # Python files only: `timeout = 30` in a TOML file parses as Python too.
    for path, text, names, members in (
        *((p, t, d.old_names, d.old_members) for p, t in old_text.items()),
        *((p, t, d.new_names, d.new_members) for p, t in new_text.items()),
    ):
        if text and path.endswith(_INDENTED):
            n, m, c = _defined_in(text)
            names |= n
            members |= m
            d.receivers |= c
    return d


def renamed_modules(files: list[FileDiff]) -> dict[str, str]:
    """Old dotted module name -> new, for each Python file the PR renames."""
    out: dict[str, str] = {}
    for fd in files:
        if fd.old_path and fd.old_path != fd.path:
            old = {m for m in _module_names(fd.old_path) if "." in m}
            new = {m for m in _module_names(fd.path) if "." in m}
            if old and new:
                out[old.pop()] = new.pop()
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
    old_text: Mapping[str, str | None],
    new_text: Mapping[str, str | None],
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
    # A rename must swap names this repository defines (FND-5316).
    defined = definitions(files, old_text, new_text)
    moves = renamed_modules(files)

    # Pass 1: which token swaps recur across files? A swap set is a rename only
    # when it is small and shows up in several files; one-off swaps are edits.
    swap_files: Counter[tuple[str, str]] = Counter()
    for fd in files:
        seen: set[tuple[str, str]] = set()
        for h in fd.hunks:
            s = hunk_swaps(h, defined)
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
            elif fd.path.endswith(".py") and hunk_imports_only(h, moves):
                t.note("imports only", fd.path)
            elif (s := hunk_swaps(h, defined)) is not None and s <= rename_set:
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
