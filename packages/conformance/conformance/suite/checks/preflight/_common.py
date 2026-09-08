"""Shared parsing and detection helpers for the preflight-gate checks (P032–P035).

One :class:`Source` per scanned file carries the parsed tree, import aliases,
suppression directives, and import provenance so the four rule passes share a
single parse and a single import walk.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, _parse_directives
from conformance.suite.checks.prescriptions._decorator_provenance import (
    _SDK_CONTRACT_MODULE_PREFIXES,
    ImportProvenance,
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
    _annotation_terminal_name,
    _get_non_self_params,
    _iter_class_body_methods,
)

_PREFLIGHT_INPUT = "PreflightInput"
_PREFLIGHT_CHECK = "PreflightCheck"


@dataclass(frozen=True)
class Source:
    """A parsed source file plus the per-file context every rule pass needs."""

    path: Path
    rel: str
    tree: ast.Module
    aliases: dict[str, str]
    directives: dict[int, _IgnoreDirective]
    prov: ImportProvenance


@dataclass(frozen=True)
class Registry:
    """Cross-file registry built once in ``scan_all`` and shared by all passes."""

    sources: tuple[Source, ...]
    by_name: dict[str, ClassRecord]
    aliases_by_rel: dict[str, dict[str, str]]
    parse_failures: tuple[str, ...] = ()


def build_registry(paths: list[Path], root: Path) -> Registry:
    """Parse *paths* once, building the shared :class:`Registry`."""
    sources: list[Source] = []
    parse_failures: list[str] = []
    by_name: dict[str, ClassRecord] = {}
    aliases_by_rel: dict[str, dict[str, str]] = {}

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            parse_failures.append(str(path.relative_to(root)))
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            parse_failures.append(str(path.relative_to(root)))
            continue
        if not isinstance(tree, ast.Module):
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        aliases = collect_import_aliases(tree)
        sources.append(
            Source(
                path=path,
                rel=rel,
                tree=tree,
                aliases=aliases,
                directives=_parse_directives(text),
                prov=collect_import_provenance(tree),
            )
        )
        aliases_by_rel[rel] = aliases
        for rec in collect_classes(tree, rel, aliases):
            by_name.setdefault(rec.name, rec)

    return Registry(
        sources=tuple(sources),
        by_name=by_name,
        aliases_by_rel=aliases_by_rel,
        parse_failures=tuple(parse_failures),
    )


def effective_task_name(
    func: ast.FunctionDef | ast.AsyncFunctionDef, dec: ast.expr
) -> str | None:
    """Return the activity name a ``@task`` registers, or ``None`` if unverifiable.

    An explicit literal ``@task(name="...")`` wins; a bare ``@task`` / ``@task()``
    falls back to the method name (mirrors ``application_sdk/app/task.py``). A
    non-literal ``name=<expr>`` cannot be resolved statically → ``None``.
    """
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg == "name":
                v = kw.value
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    return v.value
                return None
    return func.name


def task_decorator(
    func: ast.FunctionDef | ast.AsyncFunctionDef, prov: ImportProvenance
) -> ast.expr | None:
    """Return the SDK ``@task`` decorator on *func*, if any."""
    for dec in func.decorator_list:
        if is_task_decorator(dec, prov):
            return dec
    return None


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def has_preflight_token(name: str) -> bool:
    """True if ``preflight`` appears as a whole token in *name* (snake or kebab)."""
    return "preflight" in _TOKEN_SPLIT.split(name.lower())


def _first_param_annotation_name(
    func: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str]
) -> str | None:
    params = _get_non_self_params(func)
    if not params or params[0].annotation is None:
        return None
    name = _annotation_terminal_name(params[0].annotation)
    return aliases.get(name, name) if name else None


def find_preflight_check_sites(
    reg: Registry,
) -> list[tuple[Source, ast.AsyncFunctionDef]]:
    """Return ``(source, method_node)`` for every ``Handler.preflight_check`` override.

    A method qualifies when it is an ``async def preflight_check`` whose enclosing
    class derives from an SDK Handler base, or whose first non-self parameter is
    annotated ``PreflightInput`` — either signal alone is a near-unambiguous marker.
    """
    sites: list[tuple[Source, ast.AsyncFunctionDef]] = []
    from ._contracts import _Checker, _qualified

    resolver = _Checker(reg)

    def derives(src, cls, seen=frozenset()):
        if id(cls) in seen:
            return False
        for base in cls.bases:
            name = _qualified(src, base)
            if name.startswith("application_sdk.handler.") and name.rsplit(".", 1)[
                -1
            ] in {"Handler", "DefaultHandler"}:
                return True
            resolved = resolver.symbol(src, base)
            if (
                resolved is not None
                and isinstance(resolved[1], ast.ClassDef)
                and derives(*resolved, seen | {id(cls)})
            ):
                return True
        return False

    for src in reg.sources:
        for cls in _class_defs(src.tree):
            is_handler = derives(src, cls)
            for func in _iter_class_body_methods(cls):
                if func.name != "preflight_check" or not isinstance(
                    func, ast.AsyncFunctionDef
                ):
                    continue
                if (
                    is_handler
                    or _first_param_annotation_name(func, src.aliases)
                    == _PREFLIGHT_INPUT
                ):
                    sites.append((src, func))
        functions = {
            node.name: node
            for node in src.tree.body
            if isinstance(node, ast.AsyncFunctionDef)
        }
        for node in src.tree.body:
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "preflight_check"
            ):
                if (
                    Path(src.rel).name == "handler.py"
                    or _first_param_annotation_name(node, src.aliases)
                    == _PREFLIGHT_INPUT
                ):
                    sites.append((src, node))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                target = functions.get(node.value.id)
                if target is not None:
                    for binding in node.targets:
                        if isinstance(binding, ast.Name):
                            functions[binding.id] = target
                            if (
                                binding.id == "preflight_check"
                                and Path(src.rel).name == "handler.py"
                            ):
                                sites.append((src, target))
    return sites


def sdk_preflightcheck_locals(tree: ast.Module) -> frozenset[str]:
    """Local names bound to the SDK ``PreflightCheck`` via ``from … import PreflightCheck``."""
    locals_: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(
                module == p or module.startswith(p + ".")
                for p in _SDK_CONTRACT_MODULE_PREFIXES
            ):
                for alias in node.names:
                    if alias.name == _PREFLIGHT_CHECK:
                        locals_.add(alias.asname or alias.name)
    return frozenset(locals_)


def is_preflightcheck_call(
    func: ast.expr, local_names: frozenset[str], module_aliases: frozenset[str]
) -> bool:
    """True if a call's callee resolves to the SDK ``PreflightCheck`` constructor."""
    if isinstance(func, ast.Name):
        return func.id in local_names
    if isinstance(func, ast.Attribute):
        return (
            func.attr == _PREFLIGHT_CHECK
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        )
    return False


def collect_entrypoint_input_contract_names(reg: Registry) -> frozenset[str]:
    """Class names of every entrypoint *input* contract (outputs excluded).

    Fork of ``_entrypoint_contract_fields.collect_entrypoint_contract_names`` with
    the ``func.returns`` (output) branch dropped, so the gate-path metadata parity
    check compares only against what the extraction input actually carries.
    """
    contracts: set[str] = set()
    app_cache: dict[str, bool | None] = {}
    for src in reg.sources:
        for cls in _class_defs(src.tree):
            for func in _iter_class_body_methods(cls):
                is_ep = False
                if any(
                    is_entrypoint_decorator(d, src.prov) for d in func.decorator_list
                ):
                    is_ep = True
                elif any(is_task_decorator(d, src.prov) for d in func.decorator_list):
                    continue
                elif func.name == "run" and isinstance(func, ast.AsyncFunctionDef):
                    for base in cls.bases:
                        bname = (
                            base.id
                            if isinstance(base, ast.Name)
                            else getattr(base, "attr", None)
                        )
                        if bname is None:
                            continue
                        bname = src.aliases.get(bname, bname)
                        if (
                            bname == "App"
                            or resolve_ancestor(
                                bname, "App", reg.by_name, app_cache, set()
                            )
                            is True
                        ):
                            is_ep = True
                            break
                if not is_ep:
                    continue
                non_self = _get_non_self_params(func)
                if non_self and non_self[0].annotation is not None:
                    name = _annotation_terminal_name(non_self[0].annotation)
                    if name:
                        contracts.add(src.aliases.get(name, name))
    return frozenset(contracts)


def norm_key(key: str) -> str:
    """Fold hyphen/underscore so ``include-database-regex`` == ``include_database_regex``."""
    return key.replace("-", "_")


def _class_defs(tree: ast.Module) -> list[ast.ClassDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def iter_function_nodes(func: ast.AST):
    """Visit executed syntax without descending into nested function definitions."""
    for child in ast.iter_child_nodes(func):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        yield child
        yield from iter_function_nodes(child)


def reachable_preflight_sites(reg: Registry):
    """Follow resolved module helpers and self methods across app sources."""
    from ._contracts import _Checker

    resolver = _Checker(reg)
    pending = list(find_preflight_check_sites(reg))
    visited = set()
    while pending:
        src, func = pending.pop(0)
        key = (src.rel, func.lineno)
        if key in visited:
            continue
        visited.add(key)
        yield src, func
        bound = {arg.arg for arg in func.args.args}
        bound.update(
            n.id
            for n in iter_function_nodes(func)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        )
        for node in iter_function_nodes(func):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in bound:
                    continue
                target = resolver.helper(src, func, node)
                if target is not None:
                    pending.append(target)


def entrypoint_contracts(reg: Registry) -> dict[str, str | None]:
    """Resolve literal SDK entrypoint names to their input annotations."""
    result = {}
    for src in reg.sources:
        for node in ast.walk(src.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not is_entrypoint_decorator(dec, src.prov):
                    continue
                name = node.name
                if isinstance(dec, ast.Call):
                    named = next(
                        (kw.value for kw in dec.keywords if kw.arg == "name"), None
                    )
                    if named is not None:
                        if not isinstance(named, ast.Constant) or not isinstance(
                            named.value, str
                        ):
                            continue
                        name = named.value
                result[name.replace("-", "_")] = _first_param_annotation_name(
                    node, src.aliases
                )
    return result


def contracts_for_site(reg: Registry, src: Source) -> frozenset[str]:
    """Select a convention callback's contract before falling back to app contracts."""
    parts = Path(src.rel).parts
    contracts = entrypoint_contracts(reg)
    if len(parts) >= 3 and parts[-1] == "handler.py" and parts[-2] in contracts:
        name = contracts[parts[-2]]
        return frozenset({name}) if name else frozenset()
    return collect_entrypoint_input_contract_names(reg)


def coverage_findings(reg: Registry):
    """Report syntactically declared hooks and contracts that cannot be analyzed."""
    from conformance.suite.checks._ast_common import make_finding

    findings = [
        make_finding(
            filename=rel,
            rule_id="P065",
            node=ast.Pass(lineno=1, col_offset=0),
            message="Source could not be parsed or read; preflight analysis is incomplete.",
            directives={},
        )
        for rel in reg.parse_failures
    ]
    sites = find_preflight_check_sites(reg)
    visited = {(src.rel, func.lineno) for src, func in sites}
    for src in reg.sources:
        for node in ast.walk(src.tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "preflight_check"
                and (src.rel, node.lineno) not in visited
                and not any(
                    is_task_decorator(dec, src.prov) for dec in node.decorator_list
                )
            ):
                findings.append(
                    make_finding(
                        filename=src.rel,
                        rule_id="P065",
                        node=node,
                        message="Declared preflight_check is not resolved as a supported async SDK handler; analysis is incomplete.",
                        directives=src.directives,
                    )
                )
            elif (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "preflight_check"
                    for t in node.targets
                )
                and not any(s is src for s, _ in sites)
            ):
                findings.append(
                    make_finding(
                        filename=src.rel,
                        rule_id="P065",
                        node=node,
                        message="Dynamic preflight callback binding is unresolved; register behavioral scenarios and use a statically resolvable callback.",
                        directives=src.directives,
                    )
                )
    for src, func in sites:
        contracts = contracts_for_site(reg, src)
        if contracts and any(name not in reg.by_name for name in contracts):
            findings.append(
                make_finding(
                    filename=src.rel,
                    rule_id="P065",
                    node=func,
                    message="Preflight input contract fields are unresolved; metadata parity has not been evaluated.",
                    directives=src.directives,
                )
            )
    return findings
