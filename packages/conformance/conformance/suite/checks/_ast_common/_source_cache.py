"""Run-scoped cache of file text, parsed trees and discovery walks.

A full conformance run used to parse each source about ten times: every check
read and parsed every file it scanned, and the E-series resolvers re-parsed the
modules a file imports once per importing file.  Within one run the repository
does not change, so one read and one parse per file is enough.

The cache lives only inside :func:`source_cache`.  The runner opens one around
each pass over a tree, and a check's ``scan_all`` may open one too; a nested
call joins the outer cache.  Outside it every helper here reads and parses
afresh, so a test that rewrites a file between two scans sees the new text.

The cost is memory: every tree stays alive until the block exits.  On the SDK
itself (about 2,700 files) a full run peaks around 0.8 GB higher than without
the cache; an app repo is a small fraction of that.

Trees are shared between checks, so a check must not mutate a tree it gets
from here.  A checker that rewrites a tree (``ast.NodeTransformer``) must work
on a ``copy.deepcopy`` of it.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SourceFile",
    "cached_walk",
    "load_source",
    "parse_module",
    "read_text",
    "source_cache",
]


@dataclass(frozen=True)
class SourceFile:
    """A Python file read as UTF-8 and parsed."""

    path: Path
    text: str
    tree: ast.Module


@dataclass
class _RunCache:
    texts: dict[tuple[Path, str], str | None] = field(default_factory=dict)
    trees: dict[str, ast.Module] = field(default_factory=dict)
    walks: dict[tuple[object, ...], tuple[Path, ...]] = field(default_factory=dict)


_ACTIVE: ContextVar[_RunCache | None] = ContextVar(
    "conformance_source_cache", default=None
)


@contextmanager
def source_cache() -> Iterator[None]:
    """Share reads, parses and walks for the duration of the block.

    Re-entrant: inside an open cache this joins it rather than starting a new
    one, so a ``scan_all`` that opens its own still shares the runner's.
    """
    if _ACTIVE.get() is not None:
        yield
        return
    token = _ACTIVE.set(_RunCache())
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _read_text(path: Path, encoding: str) -> str | None:
    try:
        return path.read_text(encoding=encoding)
    except (OSError, UnicodeDecodeError):
        return None


def read_text(path: Path, *, encoding: str = "utf-8") -> str | None:
    """*path* as text, or ``None`` if it cannot be read or decoded.

    Remembered for the open cache, so a file every check reads is read once.
    """
    cache = _ACTIVE.get()
    if cache is None:
        return _read_text(path, encoding)
    key = (path, encoding)
    if key not in cache.texts:
        cache.texts[key] = _read_text(path, encoding)
    return cache.texts[key]


def parse_module(text: str, filename: str = "<unknown>") -> ast.Module:
    """``ast.parse(text, filename=filename)``, shared for the open cache.

    Keyed on the source text, so two checks that read the same file get the
    same tree.  A ``SyntaxError`` (or ``ValueError`` for a NUL byte) is raised
    exactly as ``ast.parse`` raises it, and is not remembered: *filename* only
    shapes that error, so a successful tree does not depend on it.
    """
    cache = _ACTIVE.get()
    if cache is None:
        return ast.parse(text, filename=filename)
    tree = cache.trees.get(text)
    if tree is None:
        tree = ast.parse(text, filename=filename)
        cache.trees[text] = tree
    return tree


def load_source(path: Path) -> SourceFile | None:
    """Read *path* as UTF-8 and parse it, or ``None`` if either step fails.

    Unreadable, undecodable and unparseable files all return ``None``; the
    runner reports the unparseable ones separately (see :mod:`._parse`).
    """
    text = read_text(path)
    if text is None:
        return None
    try:
        tree = parse_module(text, filename=str(path))
    except (SyntaxError, ValueError):
        return None
    return SourceFile(path=path, text=text, tree=tree)


def cached_walk(key: tuple[object, ...], walk: Callable[[], list[Path]]) -> list[Path]:
    """``walk()``, remembered under *key* for the open cache (if any).

    Returns a fresh list each call, so a caller that edits it does not change
    what the next caller gets.
    """
    cache = _ACTIVE.get()
    if cache is None:
        return walk()
    if key not in cache.walks:
        cache.walks[key] = tuple(walk())
    return list(cache.walks[key])
