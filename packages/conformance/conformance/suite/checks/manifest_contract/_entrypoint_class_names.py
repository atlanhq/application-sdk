"""K027 EntrypointContractClassNameCollision (FND-3140) — check implementation.

The contract ledger (``contract_schema.lock.json``) keys every entrypoint
contract by its bare class name, and the class registry the ledger and B005/B006
build is first-wins by bare name. Two entrypoints that bind *different* classes
under one name therefore share a single ledger identity: one class's fields are
checked against the other's, or one contract drops out of the ledger entirely.

Before contract-toolkit named bundle classes per entrypoint, every bundle
``app/generated/<entrypoint>/_input.py`` declared ``class AppInputContract``,
so an app binding those generated classes directly collided by construction.

Each entrypoint's Input and Output annotation is resolved through imports
(absolute, relative, ``import x as y``, ``from pkg import module``), module-level
rebindings (``AppInputContract = CrawlerInputContract``) and string annotations
to the in-repo ``ClassDef`` that declares it. A binding is visible to the ledger
under two names: the import-de-aliased name the annotation refers to, and the
declaring class's own name. A collision is one such name reached from two or
more distinct declarations. SDK classes and anything not declared in the repo
are never bindings: they are one class everywhere, and the ledger does not
record them.
"""

from __future__ import annotations

import ast
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from conformance.suite.checks._ast_common import (
    SDK_APP_BASE_NAMES,
    _IgnoreDirective,
    _parse_directives,
    make_finding,
)
from conformance.suite.checks._entrypoint_contract_classes import _extract_wire_name
from conformance.suite.checks._entrypoint_contract_fields import _base_name
from conformance.suite.checks.prescriptions._contract_common import (
    _unwrap_annotated,
    _unwrap_optional_node,
)
from conformance.suite.checks.prescriptions._decorator_provenance import (
    collect_import_provenance,
    is_entrypoint_decorator,
    is_task_decorator,
)
from conformance.suite.checks.prescriptions._error_code_prefix import (
    ClassRecord,
    collect_classes,
    collect_import_aliases,
    resolve_ancestor,
)
from conformance.suite.checks.prescriptions._typed_boundaries import (
    _get_non_self_params,
    _iter_class_body_methods,
)
from conformance.suite.schema.findings import Finding

_RULE_ID = "K027"
_SDK_PACKAGE = "application_sdk"
_MAX_DEPTH = 12


@dataclass
class _ModuleInfo:
    rel: str
    module: str
    is_package: bool
    tree: ast.Module
    classes: set[str] = field(default_factory=set)
    rebinds: dict[str, ast.expr] = field(default_factory=dict)
    from_imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    module_imports: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Binding:
    file: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    entrypoint: str
    role: str
    ref_name: str
    decl_file: str
    decl_name: str

    @property
    def decl(self) -> tuple[str, str]:
        return (self.decl_file, self.decl_name)


def _module_name(rel: str) -> tuple[str, bool]:
    parts = Path(rel).with_suffix("").parts
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _absolute_module(info: _ModuleInfo, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = info.module.split(".") if info.is_package else info.module.split(".")[:-1]
    if node.level > 1:
        package = package[: len(package) - (node.level - 1)]
    return ".".join([*package, node.module] if node.module else package)


def _index_module(info: _ModuleInfo) -> None:
    for stmt in info.tree.body:
        if isinstance(stmt, ast.ClassDef):
            info.classes.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    info.rebinds[target.id] = stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            info.rebinds[stmt.target.id] = stmt.value
        elif isinstance(stmt, ast.ImportFrom):
            module = _absolute_module(info, stmt)
            for alias in stmt.names:
                info.from_imports[alias.asname or alias.name] = (module, alias.name)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    info.module_imports[alias.asname] = alias.name
                else:
                    root = alias.name.split(".")[0]
                    info.module_imports[root] = root


class _Resolver:
    def __init__(self, modules: dict[str, _ModuleInfo]) -> None:
        self._by_rel = {m.rel: m for m in modules.values()}
        self._modules = modules

    def module_for(self, dotted: str) -> _ModuleInfo | None:
        return self._modules.get(dotted) or self._modules.get(f"src.{dotted}")

    def resolve_name(
        self, rel: str, name: str, depth: int = 0
    ) -> tuple[str, str] | None:
        info = self._by_rel.get(rel)
        if info is None or depth > _MAX_DEPTH:
            return None
        if name in info.classes:
            return (rel, name)
        if name in info.rebinds:
            return self.resolve_expr(rel, info.rebinds[name], depth + 1)
        if name in info.from_imports:
            module, orig = info.from_imports[name]
            if _is_sdk_module(module):
                return None
            target = self.module_for(module)
            if target is None:
                return None
            return self.resolve_name(target.rel, orig, depth + 1)
        return None

    def resolve_module(self, rel: str, expr: ast.expr) -> _ModuleInfo | None:
        info = self._by_rel.get(rel)
        if info is None:
            return None
        chain: list[str] = []
        cur = expr
        while isinstance(cur, ast.Attribute):
            chain.insert(0, cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            return None
        root = cur.id
        if root in info.module_imports:
            base = info.module_imports[root]
        elif root in info.from_imports:
            module, orig = info.from_imports[root]
            base = f"{module}.{orig}" if module else orig
        else:
            return None
        return self.module_for(".".join([base, *chain]))

    def is_sdk_app_base(self, rel: str, expr: ast.expr) -> bool:
        info = self._by_rel.get(rel)
        if info is None:
            return False
        if isinstance(expr, ast.Name):
            if expr.id in info.classes or expr.id in info.rebinds:
                return False
            origin = info.from_imports.get(expr.id)
            return (
                origin is not None
                and _is_sdk_module(origin[0])
                and origin[1] in SDK_APP_BASE_NAMES
            )
        if isinstance(expr, ast.Attribute) and expr.attr in SDK_APP_BASE_NAMES:
            cur = expr.value
            chain: list[str] = []
            while isinstance(cur, ast.Attribute):
                chain.insert(0, cur.attr)
                cur = cur.value
            if not isinstance(cur, ast.Name):
                return False
            module = info.module_imports.get(cur.id)
            if module is None and cur.id in info.from_imports:
                parent, orig = info.from_imports[cur.id]
                module = f"{parent}.{orig}" if parent else orig
            return module is not None and _is_sdk_module(".".join([module, *chain]))
        return False

    def reaches_sdk_app(
        self, rel: str, expr: ast.expr, seen: set[tuple[str, str]] | None = None
    ) -> bool:
        if self.is_sdk_app_base(rel, expr):
            return True
        decl = self.resolve_expr(rel, expr)
        seen = set() if seen is None else seen
        if decl is None or decl in seen or len(seen) > _MAX_DEPTH:
            return False
        seen.add(decl)
        info = self._by_rel[decl[0]]
        node = next(
            (
                s
                for s in info.tree.body
                if isinstance(s, ast.ClassDef) and s.name == decl[1]
            ),
            None,
        )
        return node is not None and any(
            self.reaches_sdk_app(decl[0], base, seen) for base in node.bases
        )

    def resolve_expr(
        self, rel: str, expr: ast.expr, depth: int = 0
    ) -> tuple[str, str] | None:
        if depth > _MAX_DEPTH:
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                parsed = ast.parse(expr.value, mode="eval").body
            except SyntaxError:
                return None
            return self.resolve_expr(rel, parsed, depth + 1)
        if isinstance(expr, ast.Name):
            return self.resolve_name(rel, expr.id, depth)
        if isinstance(expr, ast.Attribute):
            target = self.resolve_module(rel, expr.value)
            if target is None:
                return None
            return self.resolve_name(target.rel, expr.attr, depth + 1)
        return None


def _is_sdk_module(module: str) -> bool:
    return module == _SDK_PACKAGE or module.startswith(f"{_SDK_PACKAGE}.")


def _unwrap(node: ast.expr) -> ast.expr:
    inner = node
    while True:
        unwrapped = _unwrap_annotated(_unwrap_optional_node(inner))
        if unwrapped is inner:
            return inner
        inner = unwrapped


def _ref_name(expr: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        try:
            expr = ast.parse(expr.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(expr, ast.Name):
        return aliases.get(expr.id, expr.id)
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _entrypoint_methods(
    info: _ModuleInfo,
    resolver: _Resolver,
    by_name: dict[str, ClassRecord],
    app_cache: dict[str, bool | None],
    aliases: dict[str, str],
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]]:
    tree = info.tree
    prov = collect_import_provenance(tree)
    found: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef):
            continue
        for func in _iter_class_body_methods(class_node):
            deco = next(
                (d for d in func.decorator_list if is_entrypoint_decorator(d, prov)),
                None,
            )
            if deco is not None:
                wire_name, unresolved = _extract_wire_name(deco, func.name)
                found.append(
                    (func, wire_name if not unresolved and wire_name else func.name)
                )
                continue
            if any(is_task_decorator(d, prov) for d in func.decorator_list):
                continue
            if func.name != "run" or not isinstance(func, ast.AsyncFunctionDef):
                continue
            for base in class_node.bases:
                bname = _base_name(base)
                if bname is None:
                    continue
                bname = aliases.get(bname, bname)
                if (
                    bname == "App"
                    or resolve_ancestor(bname, "App", by_name, app_cache, set()) is True
                    or resolver.reaches_sdk_app(info.rel, base)
                ):
                    found.append((func, "run"))
                    break
    return found


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Flag entrypoint contracts that share a bare class name across declarations."""
    modules: dict[str, _ModuleInfo] = {}
    directives: dict[str, dict[int, _IgnoreDirective]] = {}
    aliases_by_rel: dict[str, dict[str, str]] = {}
    by_name: dict[str, ClassRecord] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        module, is_package = _module_name(rel)
        info = _ModuleInfo(rel=rel, module=module, is_package=is_package, tree=tree)
        _index_module(info)
        modules[module] = info
        directives[rel] = _parse_directives(text)
        aliases = collect_import_aliases(tree)
        aliases_by_rel[rel] = aliases
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)

    resolver = _Resolver(modules)
    app_cache: dict[str, bool | None] = {}
    bindings: list[_Binding] = []
    for info in modules.values():
        aliases = aliases_by_rel[info.rel]
        for func, entrypoint in _entrypoint_methods(
            info, resolver, by_name, app_cache, aliases
        ):
            annotations: list[tuple[str, ast.expr | None]] = []
            non_self = _get_non_self_params(func)
            annotations.append(("Input", non_self[0].annotation if non_self else None))
            annotations.append(("Output", func.returns))
            for role, ann in annotations:
                if ann is None:
                    continue
                expr = _unwrap(ann)
                ref = _ref_name(expr, aliases)
                decl = resolver.resolve_expr(info.rel, expr)
                if ref is None or decl is None:
                    continue
                bindings.append(
                    _Binding(
                        file=info.rel,
                        node=func,
                        entrypoint=entrypoint,
                        role=role,
                        ref_name=ref,
                        decl_file=decl[0],
                        decl_name=decl[1],
                    )
                )

    groups: dict[str, list[_Binding]] = {}
    for b in bindings:
        for name in {b.ref_name, b.decl_name}:
            groups.setdefault(name, []).append(b)

    findings: list[Finding] = []
    seen: set[tuple[str, int, str]] = set()
    for name in sorted(groups):
        group = groups[name]
        if len({b.decl for b in group}) < 2:
            continue
        for b in group:
            key = (b.file, b.node.lineno, name)
            if key in seen:
                continue
            seen.add(key)
            others = sorted(
                {
                    f"'{o.entrypoint}' ({o.role}: {_module_name(o.decl_file)[0]}.{o.decl_name})"
                    for o in group
                    if o.decl != b.decl
                }
            )
            findings.append(_make_finding(b, name, others, directives.get(b.file, {})))
    return findings


def _make_finding(
    b: _Binding,
    name: str,
    others: list[str],
    directives: dict[int, _IgnoreDirective],
) -> Finding:
    module = _module_name(b.decl_file)[0]
    return dataclasses.replace(
        make_finding(
            filename=b.file,
            rule_id=_RULE_ID,
            node=b.node,
            message=(
                f"Entrypoint '{b.entrypoint}' binds {b.role} contract "
                f"'{module}.{b.decl_name}' under the name '{name}'. A different class "
                f"is bound under the same name by: {', '.join(others)}. "
                "The contract ledger keys contracts by bare class name, so these "
                "classes share one ledger identity and B005/B006 check each against "
                "the other's fields. Give each entrypoint's contract a unique class "
                "name: regenerate with a contract-toolkit that names bundle input "
                "classes '<Entrypoint>AppInputContract' and import or subclass that "
                "unique name instead of the 'AppInputContract' alias, or rename the "
                "app's own class. Then regenerate the contract ledger. Suppress with "
                f"'# conformance: ignore[{_RULE_ID}] <reason>' on the entrypoint "
                "method definition."
            ),
            directives=directives,
        ),
        discriminator=name,
    )
