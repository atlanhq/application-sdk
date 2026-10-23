"""A static code index: what the model would otherwise spend turns grepping for.

Built with `ast` over the checkout in seconds, never by importing or running
the code. Resolution is by bare name, the way tree-sitter repo maps (Aider)
and Greptile's graph queries work: "who calls `resolve_credential`" answers
from a reverse map, not from a shell. Bare names over-approximate, which is
the right failure direction for context (an extra caller costs tokens; a
missing one costs a bug), and every consumer caps how many it takes.
`scoped_callers` is the narrower answer, for evidence that states a fact
about one symbol rather than gathering context around it.
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
class ScopedCallers:
    """The callers of one symbol that its scope lets reach it by name, ranked
    as `callers_of` ranks them.

    `local_to` is the outermost function the symbol is defined inside, when
    it is nested: nothing outside that function can name it. `unrelated`
    counts same-name calls from files that neither define nor import a
    module-level symbol; `untyped` counts `.name` calls on a method's name
    whose receiver the index cannot type, so they may or may not be it."""

    callers: list[Symbol]
    local_to: Symbol | None = None
    unrelated: int = 0
    untyped: int = 0


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
    imports: dict[str, list[str]] = field(
        default_factory=dict
    )  # path -> absolute dotted names it imports ("a.b", "a.b.name", "a.b.*")

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
        return _ranked([self.symbols[q] for q in seen if q in self.symbols])[:limit]

    def scoped_callers(self, sym: Symbol) -> ScopedCallers:
        """The callers of `sym` its scope allows, not every call of its bare name.

        A nested symbol is reachable by name only inside its outermost
        enclosing function. A module-level function or class is reachable from
        its own file and from files that import it, its package, or its module.
        A method is reachable from its class, its file, and files that import
        the class; other `.name` calls are counted as untyped, not listed."""
        mod, dotted = sym.qualname.split(":", 1)
        parts = dotted.split(".")
        chain = [
            self.symbols.get(f"{mod}:{'.'.join(parts[:i])}")
            for i in range(1, len(parts))
        ]
        named = [
            self.symbols[q]
            for q in self.callers.get(sym.name, [])
            if q in self.symbols and q != sym.qualname
        ]
        local_to = next((s for s in chain if s and s.kind != "class"), None)
        if local_to is not None:
            inside = [
                c
                for c in named
                if c.path == sym.path
                and (c.qualname + ".").startswith(local_to.qualname + ".")
            ]
            return ScopedCallers(
                _ranked(inside), local_to, unrelated=len(named) - len(inside)
            )
        owner = chain[-1] if sym.kind == "method" and chain else None
        target = owner or sym
        modules = _import_names(sym.path)

        def reaches(c: Symbol) -> bool:
            if c.path == sym.path:
                return True
            return any(
                _imports_symbol(i, m, target.name)
                for i in self.imports.get(c.path, [])
                for m in modules
            )

        found = [c for c in named if reaches(c)]
        rest = len(named) - len(found)
        return ScopedCallers(
            _ranked(found),
            unrelated=0 if owner else rest,
            untyped=rest if owner else 0,
        )

    def definitions(self, name: str, limit: int = 5) -> list[Symbol]:
        return [self.symbols[q] for q in self.by_name.get(name, [])[:limit]]

    def to_json(self) -> str:
        return json.dumps(
            {
                "root": self.root,
                "symbols": {k: asdict(v) for k, v in self.symbols.items()},
                "tests_for": self.tests_for,
                "imports": self.imports,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "Index":
        raw = json.loads(text)
        idx = cls(
            root=raw["root"],
            tests_for=raw.get("tests_for", {}),
            imports=raw.get("imports", {}),
        )
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


def _ranked(syms: list[Symbol]) -> list[Symbol]:
    return sorted(syms, key=lambda s: (s.path.startswith("tests/"), s.path))


def _imports_symbol(imported: str, module: str, name: str) -> bool:
    """True when the import `imported` can bring `module`'s `name` into a
    file: the module itself or a package above it (called as `mod.name`),
    the name from the module, or the name re-exported by a package above it."""
    if module == imported or module.startswith(imported + "."):
        return True
    pkg, _, last = imported.rpartition(".")
    if last not in (name, "*"):
        return False
    return module == pkg or module.startswith(pkg + ".")


def _file_imports(rel: str, tree: ast.AST) -> list[str]:
    """Every absolute dotted name `rel` imports, at any depth (a lazy import
    inside a function still reaches the module), relative imports resolved
    against the file's own package."""
    package = _module_name(rel).split(".")
    if not rel.endswith("__init__.py"):
        package = package[:-1]
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            mod = ".".join([*base, *([node.module] if node.module else [])])
            out += [f"{mod}.{a.name}" if mod else a.name for a in node.names]
    return out


def _call_name(node: ast.AST) -> str | None:
    f = node.func if isinstance(node, ast.Call) else None
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _own_calls(fn: ast.AST) -> list[ast.Call]:
    """The calls `fn` makes itself: a nested def or class is its own symbol,
    so its calls are attributed to it, not to every function around it."""
    out: list[ast.Call] = []
    todo = list(ast.iter_child_nodes(fn))
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Decorators and defaults are evaluated by the enclosing function.
            todo += [*node.decorator_list, *getattr(node, "bases", [])]
            if not isinstance(node, ast.ClassDef):
                todo += [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return out


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
    idx.imports[rel] = _file_imports(rel, tree)

    def visit(node: ast.AST, scope: list[str], in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{mod}:{'.'.join([*scope, child.name])}"
                calls: list[str] = []
                if not isinstance(child, ast.ClassDef):
                    calls = [n for n in map(_call_name, _own_calls(child)) if n]
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
