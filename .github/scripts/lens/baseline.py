"""What this PR itself added, measured against the MERGE BASE (FND-4163).

A compatibility claim ("callers of X break", "the released contract changes")
is only true of code that existed before the PR. An incremental round cannot
tell: it sees `reviewed_head..head`, so a helper added in round 1 and reshaped
in round 3 reads as a change to an existing API. Neither can the verify call,
which sees the finding's site and the round's diff, never the merge base.

So once per round this module answers it from git, with no model call:

- paths the PR adds: `status == added` in the PR's `base...head` diff, which
  GitHub already computes against the merge base;
- symbols the PR adds in files that existed: the dotted def/class names in
  the head file's AST that are absent from the merge-base file's AST.

The merge-base copy is read through the API as data, parsed, never imported
or run. It fails closed: when the merge base or a file cannot be read, no
symbol in that file counts as added, so a compatibility finding there is
judged as it was before.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field

from .diff import FileDiff
from .index import Index, Symbol, index_source


@dataclass
class PRAdditions:
    paths: list[str] = field(default_factory=list)
    symbols: dict[str, list[str]] = field(default_factory=dict)  # path -> dotted

    def render_paths(self) -> str:
        return "\n".join(self.paths)

    def render_symbols(self) -> str:
        return "\n".join(
            f"{path}: {name}" for path, names in self.symbols.items() for name in names
        )

    def added(self, path: str, sym: Symbol | None) -> str:
        """Why the subject at `path` (inside `sym`, if any) is not on the merge
        base, or "" when it is (or cannot be shown not to be)."""
        if path in self.paths:
            return f"absent on merge base: {path} (this PR adds the file)"
        if sym is None:
            return ""
        dotted = sym.qualname.split(":", 1)[1]
        names = set(self.symbols.get(path, []))
        # A method of a class this PR added is itself added.
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            if ".".join(parts[:i]) in names:
                return (
                    f"absent on merge base: {'.'.join(parts[:i])} in {path} "
                    "(this PR adds the symbol)"
                )
        return ""


def dotted_names(rel: str, text: str) -> set[str] | None:
    """Every def/class in `text` by dotted name, or None when it does not parse."""
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    idx = Index(root="")
    index_source(idx, rel, text)
    return {q.split(":", 1)[1] for q in idx.by_path.get(rel, [])}


def pr_additions(
    files: list[FileDiff],
    head_text: Callable[[str], str | None],
    base_text: Callable[[str], str | None],
) -> PRAdditions:
    """`files` is the whole PR's diff; `head_text(path)` / `base_text(path)`
    read a path at the PR head / the merge base (None when unreadable)."""
    out = PRAdditions()
    for fd in files:
        if fd.status == "added":
            out.paths.append(fd.path)
            continue
        if fd.status == "deleted" or not fd.path.endswith(".py"):
            continue
        head = head_text(fd.path)
        if not head:
            continue
        base_path = fd.old_path if fd.status == "renamed" and fd.old_path else fd.path
        old = base_text(base_path)
        if old is None:
            continue  # fail closed: nothing in this file counts as added
        before = dotted_names(base_path, old)
        after = dotted_names(fd.path, head)
        if before is None or after is None:
            continue
        new = sorted(after - before)
        if new:
            out.symbols[fd.path] = new
    out.paths.sort()
    return out
