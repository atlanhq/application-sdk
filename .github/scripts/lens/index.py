"""A static code index: what the model would otherwise spend turns grepping for.

Built with `ast` over the checkout in seconds, never by importing or running
the code. Resolution is by bare name, the way tree-sitter repo maps (Aider)
and Greptile's graph queries work: "who calls `resolve_credential`" answers
from a reverse map, not from a shell. Bare names over-approximate, which is
the right failure direction for context (an extra caller costs tokens; a
missing one costs a bug), and every consumer caps how many it takes.
"""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".worktrees",
    ".claude",
    "build",
    "dist",
    "site",
}


@dataclass
class Symbol:
    qualname: str  # "pkg.mod:Class.method"
    name: str
    kind: str  # function | method | class
    path: str
    start: int
    end: int
    signature: str
    doc: str
    calls: list[str] = field(default_factory=list)


@dataclass
class Index:
    root: str
    symbols: dict[str, Symbol] = field(default_factory=dict)
    by_name: dict[str, list[str]] = field(default_factory=dict)
    callers: dict[str, list[str]] = field(
        default_factory=dict
    )  # bare callee name -> caller qualnames
    by_path: dict[str, list[str]] = field(default_factory=dict)
    tests_for: dict[str, list[str]] = field(
        default_factory=dict
    )  # source path -> test paths

    # ---- queries -------------------------------------------------------
    def enclosing(self, path: str, line: int) -> Symbol | None:
        best: Symbol | None = None
        for q in self.by_path.get(path, []):
            s = self.symbols[q]
            if s.start <= line <= s.end and (best is None or s.start >= best.start):
                best = s
        return best

    def callers_of(self, name: str, limit: int = 5) -> list[Symbol]:
        seen = self.callers.get(name, [])
        # Prefer callers outside tests and outside the callee's own file: those are the contracts a change can break.
        out = [self.symbols[q] for q in seen if q in self.symbols]
        out.sort(key=lambda s: (s.path.startswith("tests/"), s.path))
        return out[:limit]

    def definitions(self, name: str, limit: int = 5) -> list[Symbol]:
        return [self.symbols[q] for q in self.by_name.get(name, [])[:limit]]

    def to_json(self) -> str:
        return json.dumps(
            {
                "root": self.root,
                "symbols": {k: asdict(v) for k, v in self.symbols.items()},
                "tests_for": self.tests_for,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "Index":
        raw = json.loads(text)
        idx = cls(root=raw["root"], tests_for=raw.get("tests_for", {}))
        for q, s in raw["symbols"].items():
            idx._add(Symbol(**s))
        idx._link()
        return idx

    # ---- build ---------------------------------------------------------
    def _add(self, s: Symbol) -> None:
        self.symbols[s.qualname] = s
        self.by_name.setdefault(s.name, []).append(s.qualname)
        self.by_path.setdefault(s.path, []).append(s.qualname)

    def _link(self) -> None:
        self.callers = {}
        for q, s in self.symbols.items():
            for c in set(s.calls):
                self.callers.setdefault(c, []).append(q)


def _call_name(node: ast.AST) -> str | None:
    f = node.func if isinstance(node, ast.Call) else None
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, source: str
) -> str:
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}({bases})" if bases else f"class {node.name}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){ret}"


def _module_name(rel: str) -> str:
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def index_source(idx: Index, rel: str, source: str) -> None:
    """Add one file's symbols. Unparseable files are skipped, not fatal: a PR
    with a syntax error still gets reviewed on its diff."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return
    mod = _module_name(rel)

    def visit(node: ast.AST, scope: list[str], in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{mod}:{'.'.join([*scope, child.name])}"
                calls: list[str] = []
                if not isinstance(child, ast.ClassDef):
                    calls = [
                        n
                        for n in (
                            _call_name(c)
                            for c in ast.walk(child)
                            if isinstance(c, ast.Call)
                        )
                        if n
                    ]
                kind = (
                    "class"
                    if isinstance(child, ast.ClassDef)
                    else ("method" if in_class else "function")
                )
                doc = (ast.get_docstring(child) or "").strip().split("\n", 1)[0][:200]
                idx._add(
                    Symbol(
                        qualname=q,
                        name=child.name,
                        kind=kind,
                        path=rel,
                        start=min(
                            [child.lineno, *(d.lineno for d in child.decorator_list)]
                        ),
                        end=child.end_lineno or child.lineno,
                        signature=_signature(child, source),
                        doc=doc,
                        calls=calls,
                    )
                )
                visit(child, [*scope, child.name], isinstance(child, ast.ClassDef))

    visit(tree, [], False)


def _is_test(p: str) -> bool:
    return p.startswith("tests/") or "/tests/" in p


def _import_names(rel: str) -> list[str]:
    """Every dotted name a file could be imported as. Src-layout packages
    (`packages/conformance/conformance/x.py` imported as `conformance.x`)
    make the path-derived name only one candidate, so every suffix of two
    or more components is registered too; a spurious suffix only ever adds
    a test to the list, never hides one."""
    parts = _module_name(rel).split(".")
    return [".".join(parts[i:]) for i in range(len(parts) - 1)] or [parts[0]]


def _test_map(
    py_files: list[str], read: Callable[[str], str | None]
) -> dict[str, list[str]]:
    """source path -> test files that import its module. Import-based, so it
    is cheap and deterministic; a test that exercises code only indirectly is
    not listed, which is exactly the "no direct test" signal a reviewer wants."""
    modules: dict[str, str] = {}
    for p in py_files:
        if not _is_test(p):
            for name in _import_names(p):
                modules.setdefault(name, p)
    out: dict[str, list[str]] = {}
    for p in py_files:
        if not _is_test(p):
            continue
        text = read(p)
        if not text:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for n in names:
                src = modules.get(n)
                if src and p not in out.setdefault(src, []):
                    out[src].append(p)
    return out


def iter_py_files(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for f in filenames:
            if f.endswith(".py"):
                out.append(str((Path(dirpath) / f).relative_to(root)))
    return sorted(out)


def build_index(root: Path, overrides: dict[str, str] | None = None) -> Index:
    """Index the checkout at `root`. `overrides` maps a relative path to the
    PR-head text of that file, so the index reflects the change under review
    while the checkout itself stays the trusted base branch. A None-like
    empty string deletes the file from the index."""
    overrides = overrides or {}
    idx = Index(root=str(root))
    files = set(iter_py_files(root)) | {p for p in overrides if p.endswith(".py")}

    def read(rel: str) -> str | None:
        if rel in overrides:
            return overrides[rel] or None  # "" = deleted by the PR
        try:
            return (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    live = sorted(p for p in files if not (p in overrides and not overrides[p]))
    for rel in live:
        text = read(rel)
        if text:
            index_source(idx, rel, text)
    idx.tests_for = _test_map(live, read)
    idx._link()
    return idx
