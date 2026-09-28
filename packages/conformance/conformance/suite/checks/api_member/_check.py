"""P053 HostedApiMemberNotThin — check implementation.

Reads every ``[project.entry-points."atlan.app_api"]`` table, resolves each
``<pkg>:<attr>`` target to a package directory next to the declaring
``pyproject.toml``, and grades the Python sources inside it.  With no such entry
point anywhere the rule is **not evaluated**: :func:`scan` returns ``[]`` before
reading a single source file.

This rule is about which distribution the member imports, so it reads raw module
names and deliberately does not use ``_ast_common.canonical_sdk_module`` — that
helper would fold ``application_sdk_api`` (allowed) onto ``application_sdk``
(forbidden).
"""

from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _IgnoreDirective,
    _parse_directives,
    make_finding,
    make_toml_finding,
    parse_toml_suppressions,
    safe_read_text,
)
from conformance.suite.checks.app_name_alignment._contract_app_name import scan_contract
from conformance.suite.schema.findings import Finding

RULE_ID = "P053"
ENTRY_POINT_GROUP = "atlan.app_api"

_FORBIDDEN_ROOTS = ("application_sdk", "app")
_OS_READS = frozenset({"environ", "getenv", "environb", "getenvb"})


@dataclass(frozen=True)
class ApiEntryPoint:
    """One ``<name> = "<pkg>:<attr>"`` entry in an ``atlan.app_api`` table."""

    name: str
    target: str
    pyproject: Path
    line: int

    @property
    def package(self) -> str:
        """The top-level import package the entry point loads (``pkg`` of ``pkg.x:attr``)."""
        return self.target.split(":", 1)[0].strip().split(".", 1)[0]


def _entry_point_line(text: str, name: str) -> int:
    """1-based line of ``<name> =`` inside the ``atlan.app_api`` table, else the header."""
    header = re.compile(
        r"""^\s*\[\s*project\.entry-points\.(?:"atlan\.app_api"|'atlan\.app_api')\s*\]"""
    )
    in_table = False
    header_line = 1
    key = re.compile(r"""^\s*(?:"|')?""" + re.escape(name) + r"""(?:"|')?\s*=""")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if header.match(line):
            in_table, header_line = True, lineno
            continue
        if in_table and re.match(r"^\s*\[", line):
            in_table = False
        if in_table and key.match(line):
            return lineno
    return header_line


def entry_points(pyprojects: list[Path]) -> list[ApiEntryPoint]:
    """Every ``atlan.app_api`` entry point declared in *pyprojects*."""
    found: list[ApiEntryPoint] = []
    for pyproject in pyprojects:
        text = safe_read_text(pyproject)
        if text is None:
            continue
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            continue
        project = data.get("project")
        eps = project.get("entry-points") if isinstance(project, dict) else None
        group = eps.get(ENTRY_POINT_GROUP) if isinstance(eps, dict) else None
        if not isinstance(group, dict):
            continue
        for name, target in group.items():
            if isinstance(target, str):
                found.append(
                    ApiEntryPoint(
                        name=name,
                        target=target,
                        pyproject=pyproject,
                        line=_entry_point_line(text, name),
                    )
                )
    return found


def _package_roots(ep: ApiEntryPoint) -> list[Path]:
    """Where the entry point's package lives, relative to its ``pyproject.toml``.

    Checks the flat (``<dir>/<pkg>``) and ``src`` (``<dir>/src/<pkg>``) layouts,
    as a package directory or a single module.
    """
    base = ep.pyproject.parent
    roots: list[Path] = []
    for parent in (base, base / "src"):
        if (parent / ep.package).is_dir():
            roots.append(parent / ep.package)
        elif (parent / f"{ep.package}.py").is_file():
            roots.append(parent / f"{ep.package}.py")
    return roots


def _forbidden_import(module: str) -> str | None:
    for root in _FORBIDDEN_ROOTS:
        if module == root or module.startswith(root + "."):
            return root
    return None


def _import_findings(
    tree: ast.AST, file: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        for module in modules:
            root = _forbidden_import(module)
            if root is None:
                continue
            why = (
                "the consolidated API server installs atlan-application-sdk-api, "
                "not atlan-application-sdk. Import the handler surface and errors "
                "from 'application_sdk_api' instead"
                if root == "application_sdk"
                else "'app' is the worker package and imports the worker's "
                "dependency tree. Move what the handler shares with the worker "
                "into the member"
            )
            findings.append(
                make_finding(
                    filename=file,
                    rule_id=RULE_ID,
                    node=node,
                    message=(
                        f"Hosted api member imports '{module}' — {why}. Suppress "
                        f"with '# conformance: ignore[{RULE_ID}] <reason>'."
                    ),
                    directives=directives,
                )
            )
            break
    return findings


def _os_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Local names bound to the ``os`` module, and to ``os.environ``/``os.getenv``."""
    modules: set[str] = set()
    readers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    modules.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in _OS_READS:
                    readers.add(alias.asname or alias.name)
    return modules, readers


def _import_time_exprs(body: list[ast.stmt]) -> list[ast.AST]:
    """The parts of *body* that execute when the module is imported.

    Module and class bodies run at import; function bodies do not, but their
    decorators and default arguments do.  Lambdas are deferred too.
    """
    out: list[ast.AST] = []
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(stmt.decorator_list)
            out.extend(d for d in stmt.args.defaults)
            out.extend(d for d in stmt.args.kw_defaults if d is not None)
            if stmt.returns is not None:
                out.append(stmt.returns)
        elif isinstance(stmt, ast.ClassDef):
            out.extend(stmt.decorator_list)
            out.extend(stmt.bases)
            out.extend(stmt.keywords)
            out.extend(_import_time_exprs(stmt.body))
        else:
            nested: list[ast.stmt] = []
            for field in ("body", "orelse", "finalbody"):
                nested.extend(getattr(stmt, field, []) or [])
            for handler in getattr(stmt, "handlers", []) or []:
                nested.extend(handler.body)
            for case in getattr(stmt, "cases", []) or []:
                nested.extend(case.body)
            for child in ast.iter_child_nodes(stmt):
                if not isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                    out.append(child)
            out.extend(_import_time_exprs(nested))
    return out


def _walk_eager(node: ast.AST):
    """``ast.walk`` that does not descend into a lambda (its body is deferred)."""
    stack = [] if isinstance(node, ast.Lambda) else [node]
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if not isinstance(child, ast.Lambda):
                stack.append(child)


def _environment_findings(
    tree: ast.Module, file: str, directives: dict[int, _IgnoreDirective]
) -> list[Finding]:
    modules, readers = _os_bindings(tree)
    if not modules and not readers:
        return []
    findings: list[Finding] = []
    seen: set[int] = set()
    for expr in _import_time_exprs(tree.body):
        for node in _walk_eager(expr):
            hit = (
                isinstance(node, ast.Attribute)
                and node.attr in _OS_READS
                and isinstance(node.value, ast.Name)
                and node.value.id in modules
            ) or (isinstance(node, ast.Name) and node.id in readers)
            line = getattr(node, "lineno", None)
            if not hit or line in seen:
                continue
            seen.add(line)
            findings.append(
                make_finding(
                    filename=file,
                    rule_id=RULE_ID,
                    node=node,
                    message=(
                        "Hosted api member reads the environment at import time. "
                        "The consolidated API server imports the member once, in a "
                        "process whose environment belongs to the host, so the value "
                        "is absent or another app's. Read it inside the handler "
                        f"method instead. Suppress with '# conformance: "
                        f"ignore[{RULE_ID}] <reason>'."
                    ),
                    directives=directives,
                )
            )
    return findings


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _name_findings(eps: list[ApiEntryPoint], root: Path) -> list[Finding]:
    contract = scan_contract(root)
    if contract.contract_name is None:
        return []
    findings: list[Finding] = []
    for ep in eps:
        if ep.name == contract.contract_name:
            continue
        text = safe_read_text(ep.pyproject) or ""
        findings.append(
            make_toml_finding(
                rule_id=RULE_ID,
                file=_rel(ep.pyproject, root),
                line=ep.line,
                column=1,
                message=(
                    f"The '{ENTRY_POINT_GROUP}' entry point is named '{ep.name}' but "
                    f"the app is '{contract.contract_name}' "
                    f"({contract.contract_source}). The consolidated API server "
                    f"routes the handler under the entry-point name, so rename it "
                    f"to '{contract.contract_name}'."
                ),
                suppressions=parse_toml_suppressions(text),
                discriminator=ep.name,
            )
        )
    return findings


def scan(paths: list[Path], root: Path) -> list[Finding]:
    """Return P053 findings; ``[]`` (not evaluated) with no ``atlan.app_api`` entry point."""
    eps = entry_points([p for p in paths if p.name == "pyproject.toml"])
    if not eps:
        return []

    member_roots = [r.resolve() for ep in eps for r in _package_roots(ep)]
    findings: list[Finding] = []
    for path in paths:
        if path.suffix != ".py":
            continue
        resolved = path.resolve()
        if not any(resolved == r or r in resolved.parents for r in member_roots):
            continue
        text = safe_read_text(path)
        if text is None:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        rel = _rel(path, root)
        directives = _parse_directives(text)
        findings.extend(_import_findings(tree, rel, directives))
        findings.extend(_environment_findings(tree, rel, directives))

    findings.extend(_name_findings(eps, root))
    return findings
