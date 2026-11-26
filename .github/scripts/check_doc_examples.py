"""Gate the ```python examples under docs/ on parsing and on their SDK imports.

Nothing executes the docs' code blocks, so they drifted: an audit found blocks
that did not parse and imports of `application_sdk` names that no longer
exist (FND-4272). This check reads every ```python block in docs/**/*.md and
fails when

* the block does not parse (``ast.parse``), or
* an ``import application_sdk...`` / ``from application_sdk... import X``
  names a module that is not in the source tree, or an ``X`` the module does
  not define, import, or lazily re-export.

Resolution is static, against the files under ``application_sdk/``: nothing is
imported, so the check needs no dependencies. A module that defines a
PEP 562 ``__getattr__`` re-exports lazily; for those, the string keys of its
top-level dict literals (the ``_LAZY`` tables), its ``__all__``, and the
string constants its ``__getattr__`` compares ``name`` against count as
defined.

A block that shows v2 code on purpose (the "before" side of a migration
example) is skipped when the line before its opening fence is the marker::

    <!-- doc-example: v2 -->

It is still required to parse; only its imports are not resolved. In a block
that shows v2 and v3 side by side, mark the v2 import statements instead, with
a trailing comment starting ``# v2`` on the statement's first line::

    from application_sdk.services.objectstore import ObjectStore  # v2

Usage: check_doc_examples.py [--docs DIR] [--src DIR]
Exits 1 and prints one ``path:line: message`` per failure.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Iterator, Sequence

PACKAGE = "application_sdk"
V2_MARKER = "<!-- doc-example: v2 -->"
_V2_COMMENT = re.compile(r"#\s*v2\b")

_FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})\s*(?P<info>.*)$")


@dataclass(frozen=True)
class Block:
    """One fenced code block: its 1-based opening-fence line and its source."""

    line: int
    source: str
    v2: bool


def _language(info: str) -> str:
    return (
        info.strip().split(maxsplit=1)[0].lstrip(".{").rstrip("}")
        if info.strip()
        else ""
    )


def _dedent(line: str, indent: str) -> str:
    return line[len(indent) :] if line.startswith(indent) else line.lstrip(" \t")


def python_blocks(text: str) -> Iterator[Block]:
    """Yield the ```python / ```py blocks of a markdown document.

    Fences may be indented (inside admonitions and tabs); the block's lines are
    dedented by the opening fence's indent.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _FENCE_OPEN.match(lines[i])
        if match is None:
            i += 1
            continue
        indent, fence = match["indent"], match["fence"]
        close = re.compile(rf"^[ \t]*{re.escape(fence[0])}{{{len(fence)},}}\s*$")
        start = i
        i += 1
        body: list[str] = []
        while i < len(lines) and not close.match(lines[i]):
            body.append(_dedent(lines[i], indent))
            i += 1
        i += 1  # past the closing fence
        if _language(match["info"]) in ("python", "py"):
            previous = lines[start - 1].strip() if start else ""
            yield Block(start + 1, "\n".join(body) + "\n", previous == V2_MARKER)


def _module_path(src: Path, module: str) -> Path | None:
    """Return the file defining ``module`` under ``src``, or None."""
    base = src.joinpath(*module.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


def _string_keys(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Dict):
        return {
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    return set()


def _strings(node: ast.AST) -> set[str]:
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _top_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Module statements, descending into if/try/with blocks but not defs."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.If):
            yield from _top_level(stmt.body)
            yield from _top_level(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            yield from _top_level(stmt.body)
            for handler in stmt.handlers:
                yield from _top_level(handler.body)
            yield from _top_level(stmt.orelse)
            yield from _top_level(stmt.finalbody)
        elif isinstance(stmt, ast.With):
            yield from _top_level(stmt.body)


def _target_names(target: ast.expr) -> set[str]:
    return {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}


@cache
def module_names(path: Path) -> frozenset[str]:
    """Names a module defines at top level, including lazy re-exports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    lazy: set[str] = set()
    has_getattr = False
    for stmt in _top_level(tree.body):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
            if stmt.name == "__getattr__":
                has_getattr = True
                for node in ast.walk(stmt):
                    if isinstance(node, ast.Compare):
                        lazy |= _strings(node)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                names |= _target_names(target)
                if isinstance(target, ast.Name) and target.id == "__all__":
                    lazy |= _strings(stmt.value)
            lazy |= _string_keys(stmt.value)
        elif isinstance(stmt, ast.AnnAssign):
            names |= _target_names(stmt.target)
            if stmt.value is not None:
                lazy |= _string_keys(stmt.value)
        elif isinstance(stmt, ast.AugAssign):
            names |= _target_names(stmt.target)
    return frozenset(names | lazy) if has_getattr else frozenset(names)


def import_problems(source: str, src: Path) -> Iterator[tuple[int, str]]:
    """Yield (line in block, message) for each unresolvable SDK import.

    ``source`` must parse. Imports marked with a trailing ``# v2`` are skipped.
    """
    lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and _V2_COMMENT.search(
            lines[node.lineno - 1]
        ):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (
                    alias.name.split(".")[0] == PACKAGE
                    and _module_path(src.parent, alias.name) is None
                ):
                    yield node.lineno, f"no module {alias.name}"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] != PACKAGE:
                continue
            path = _module_path(src.parent, node.module)
            if path is None:
                yield node.lineno, f"no module {node.module}"
                continue
            defined = module_names(path)
            for alias in node.names:
                if alias.name == "*" or alias.name in defined:
                    continue
                if path.name == "__init__.py" and _module_path(
                    src.parent, f"{node.module}.{alias.name}"
                ):
                    continue
                yield node.lineno, f"{node.module} has no name {alias.name}"


def check_file(path: Path, src: Path) -> Iterator[str]:
    """Yield one ``path:line: message`` per failing block in ``path``."""
    for block in python_blocks(path.read_text(encoding="utf-8")):
        try:
            ast.parse(block.source)
        except SyntaxError as err:
            line = block.line + (err.lineno or 0)
            yield f"{path}:{line}: does not parse: {err.msg}"
            continue
        if block.v2:
            continue
        for lineno, message in import_problems(block.source, src):
            yield f"{path}:{block.line + lineno}: {message}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate the docs python examples.")
    parser.add_argument("--docs", type=Path, default=Path("docs"))
    parser.add_argument("--src", type=Path, default=Path(PACKAGE))
    args = parser.parse_args(argv)
    failures = [
        failure
        for path in sorted(args.docs.rglob("*.md"))
        for failure in check_file(path, args.src)
    ]
    for failure in failures:
        print(failure)
    if failures:
        print(
            f"\n{len(failures)} doc example problem(s). Fix the example, or mark an "
            f"intentional v2 block with `{V2_MARKER}` on the line before its fence.",
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
