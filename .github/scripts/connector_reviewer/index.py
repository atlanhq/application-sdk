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
import re
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
    # "Type.name" per `.name()` call whose receiver's class is known at the
    # call site ("self.name" for self, cls and super()); see `_typed_calls`.
    typed_calls: list[str] = field(default_factory=list)


@dataclass
class ScopedCallers:
    """The callers of one symbol that its scope lets reach it by name, ranked
    as `callers_of` ranks them.

    `local_to` is the outermost function the symbol is defined inside, when
    it is nested: nothing outside that function can name it. `owner` is a
    method's class. `unrelated` counts same-name calls from files that
    neither define nor import a module-level symbol; `untyped` counts
    `.name()` calls whose receiver the index cannot tie to the method's
    class, so they may or may not be it."""

    callers: list[Symbol]
    local_to: Symbol | None = None
    owner: Symbol | None = None
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
    aliases: dict[str, dict[str, str]] = field(
        default_factory=dict
    )  # path -> {local name: absolute dotted name} for `import ... as`

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

        A nested function or class is reachable by name only inside its
        outermost enclosing function. A module-level function or class is
        reachable from its own file and from files that import it (under any
        alias), its package, or its module. A method is reachable from any
        instance, even of a local class, so a call counts only when its
        receiver is tied to the class (see `_typed_calls`); every other
        `.name()` call is counted as untyped, not listed."""
        named = [
            self.symbols[q]
            for q in self.callers.get(sym.name, [])
            if q in self.symbols and q != sym.qualname
        ]
        chain = self._parents(sym)
        if sym.kind == "method" and chain and chain[-1]:
            return self._method_callers(sym, chain[-1], named)
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
        found = [c for c in named if c.path == sym.path or self._reaches(c.path, sym)]
        unrelated = len(named) - len(found)
        found += [
            self.symbols[q]
            for path in self.aliases
            for local in self._local_names(path, sym) - {sym.name}
            for q in self.callers.get(local, [])
            if q in self.symbols and self.symbols[q].path == path
        ]
        return ScopedCallers(_ranked(found), unrelated=unrelated)

    def _parents(self, sym: Symbol) -> list[Symbol | None]:
        mod, dotted = sym.qualname.split(":", 1)
        parts = dotted.split(".")
        return [
            self.symbols.get(f"{mod}:{'.'.join(parts[:i])}")
            for i in range(1, len(parts))
        ]

    def _reaches(self, path: str, sym: Symbol) -> bool:
        """True when a file's imports can bring module-level `sym` into it."""
        modules = _import_names(sym.path)
        return any(
            _imports_symbol(i, m, sym.name)
            for i in self.imports.get(path, [])
            for m in modules
        )

    def _local_names(self, path: str, sym: Symbol) -> set[str]:
        """The names module-level `sym` goes by in `path`: its own where the
        file defines or imports it, plus every `from m import sym as alias`."""
        modules = _import_names(sym.path)
        names = {sym.name} if path == sym.path or self._reaches(path, sym) else set()
        return names | {
            local
            for local, full in self.aliases.get(path, {}).items()
            if full.rpartition(".")[2] == sym.name
            and any(_imports_symbol(full, m, sym.name) for m in modules)
        }

    def _method_callers(
        self, sym: Symbol, owner: Symbol, named: list[Symbol]
    ) -> ScopedCallers:
        local_class = any(s and s.kind != "class" for s in self._parents(owner))

        def class_names(path: str) -> set[str]:
            if local_class:  # importable from nowhere: only its own file names it
                return {owner.name} if path == sym.path else set()
            return self._local_names(path, owner)

        def in_class(c: Symbol) -> bool:
            if (c.qualname + ".").startswith(owner.qualname + "."):
                return True
            k = next(
                (s for s in reversed(self._parents(c)) if s and s.kind == "class"), None
            )
            bases = k.signature.partition("(")[2] if k else ""
            return owner.name in re.findall(r"\w+", bases)

        found = [
            c
            for c in named
            if (f"self.{sym.name}" in c.typed_calls and in_class(c))
            or any(f"{t}.{sym.name}" in c.typed_calls for t in class_names(c.path))
        ]
        return ScopedCallers(
            _ranked(found), owner=owner, untyped=len(named) - len(found)
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
                "aliases": self.aliases,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> "Index":
        raw = json.loads(text)
        idx = cls(
            root=raw["root"],
            tests_for=raw.get("tests_for", {}),
            imports=raw.get("imports", {}),
            aliases=raw.get("aliases", {}),
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


def _file_imports(rel: str, tree: ast.AST) -> tuple[list[str], dict[str, str]]:
    """Every absolute dotted name `rel` imports, at any depth (a lazy import
    inside a function still reaches the module), relative imports resolved
    against the file's own package; and the local name of each one bound
    under an `as` alias."""
    package = _module_name(rel).split(".")
    if not rel.endswith("__init__.py"):
        package = package[:-1]
    out: list[str] = []
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            pairs = [(a.name, a) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            mod = ".".join([*base, *([node.module] if node.module else [])])
            pairs = [(f"{mod}.{a.name}" if mod else a.name, a) for a in node.names]
        else:
            continue
        for full, a in pairs:
            out.append(full)
            if a.asname and a.asname != a.name:
                aliases[a.asname] = full
    return out, aliases


def _call_name(node: ast.AST) -> str | None:
    f = node.func if isinstance(node, ast.Call) else None
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """The nodes `fn` evaluates itself: a nested def or class is its own
    symbol, so what its body does is attributed to it, not to every function
    around it. Its decorators, bases and defaults run in `fn`, so they stay."""
    out: list[ast.AST] = []
    todo = list(ast.iter_child_nodes(fn))
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            todo += [*node.decorator_list, *getattr(node, "bases", [])]
            if not isinstance(node, ast.ClassDef):
                todo += [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
            continue
        out.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return out


def _last(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _type_names(ann: ast.AST | None) -> set[str]:
    """The classes an annotation says a value is: `C`, `m.C`, `C | None`,
    `Optional[C]`, `Union[C, D]`, or any of those quoted. A container
    (`dict[str, C]`) is not a `C`, so it names nothing."""
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            return _type_names(ast.parse(ann.value, mode="eval").body)
        except SyntaxError:
            return set()
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return _type_names(ann.left) | _type_names(ann.right)
    if isinstance(ann, ast.Subscript) and _last(ann.value) in ("Optional", "Union"):
        sl = ann.slice
        return set().union(
            *(_type_names(e) for e in (sl.elts if isinstance(sl, ast.Tuple) else [sl]))
        )
    name = _last(ann) if ann is not None else None
    return {name} if name else set()


def _typed_calls(fn: ast.AST, nodes: list[ast.AST]) -> list[str]:
    """One "Type.name" per `.name()` call in `nodes` whose receiver's class
    the call site states: `self`, `cls` or `super()` ("self.name"), `C(...)`,
    `C` itself, or a local or parameter annotated as, or assigned from, `C(...)`.
    Anything else (`self.store.get()`, `d.get()` on an unannotated `d`) has
    no entry, so it stays an untyped receiver."""
    env: dict[str, set[str]] = {}
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = fn.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
            env[arg.arg] = _type_names(arg.annotation)
    for n in nodes:
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            env[n.target.id] = _type_names(n.annotation)
        elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            made = _last(n.value.func)
            for t in n.targets:
                if isinstance(t, ast.Name) and made:
                    env.setdefault(t.id, set()).add(made)
    out: list[str] = []
    for n in nodes:
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        v = n.func.value
        if isinstance(v, ast.Name) and v.id in ("self", "cls"):
            types = {"self"}
        elif isinstance(v, ast.Call) and _last(v.func) == "super":
            types = {"self"}
        elif isinstance(v, ast.Name):
            types = env.get(v.id, {v.id})
        elif isinstance(v, ast.Call):
            types = {t for t in [_last(v.func)] if t}
        else:
            types = set()
        out += [f"{t}.{n.func.attr}" for t in sorted(types)]
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
    idx.imports[rel], idx.aliases[rel] = _file_imports(rel, tree)

    def visit(node: ast.AST, scope: list[str], in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{mod}:{'.'.join([*scope, child.name])}"
                # A class body runs once, at definition: its calls are the class's.
                nodes = _own_nodes(child)
                own = [n for n in nodes if isinstance(n, ast.Call)]
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
                        calls=[n for n in map(_call_name, own) if n],
                        typed_calls=_typed_calls(child, nodes),
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
