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

A block that shows a v2 or deprecated API on purpose carries the
``legacy-example`` marker, on the line directly above its opening fence. Blocks
and markers are read by _doc_blocks.py, as check_docs_deprecated_imports.py
reads them::

    <!-- legacy-example: v2 "before" snippet -->

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
from functools import cache
from pathlib import Path
from typing import Iterator, Sequence

from _doc_blocks import LEGACY_MARKER, CodeBlock, iter_code_blocks

PACKAGE = "application_sdk"
#: Fence tags read as Python. Untagged fences are not: under docs/ they are
#: mostly shell output and directory trees.
PYTHON_TAGS = frozenset({"python", "py", "python3"})
_V2_COMMENT = re.compile(r"#\s*v2\b")


def python_blocks(text: str) -> Iterator[CodeBlock]:
    """Yield the Python-tagged blocks of a Markdown document."""
    return iter_code_blocks(text, PYTHON_TAGS)


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
    for stmt in _top_level(tree.body):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
            if stmt.name == "__getattr__":
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
    # A PEP 562 getter may be a def or an assignment
    # (``__getattr__ = deprecated_module_getattr(...)``); either way it is bound.
    return frozenset(names | lazy) if "__getattr__" in names else frozenset(names)


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
        source = block.body + "\n"
        try:
            ast.parse(source)
        except SyntaxError as err:
            line = block.first_line + (err.lineno or 1) - 1
            yield f"{path}:{line}: does not parse: {err.msg}"
            continue
        if block.marked:
            continue
        for lineno, message in import_problems(source, src):
            yield f"{path}:{block.first_line + lineno - 1}: {message}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gate the docs python examples.")
    parser.add_argument("--docs", type=Path, default=Path("docs"))
    parser.add_argument("--src", type=Path, default=Path(PACKAGE))
    args = parser.parse_args(argv)
    # Fail closed: a wrong --docs or --src must not pass by checking nothing.
    if not (args.src / "__init__.py").is_file():
        print(f"{args.src}: not a package directory", file=sys.stderr)
        return 2
    pages = sorted(args.docs.rglob("*.md")) if args.docs.is_dir() else []
    if not any(
        next(python_blocks(page.read_text(encoding="utf-8")), None) for page in pages
    ):
        print(f"{args.docs}: no Python examples found to check", file=sys.stderr)
        return 2
    failures = [failure for page in pages for failure in check_file(page, args.src)]
    for failure in failures:
        print(failure)
    if failures:
        print(
            f"\n{len(failures)} doc example problem(s). Fix the example, or mark an "
            f"intentional v2 block with `{LEGACY_MARKER}` on the line before its fence.",
            file=sys.stderr,
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
