"""D003 — unused dependencies and app-local constraint floors."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator, Mapping
from importlib import metadata as importlib_metadata
from pathlib import Path

from conformance.suite.checks._ast_common import (
    SuppressionsMap,
    parse_toml_suppressions,
)
from conformance.suite.checks._ast_common._discovery import (
    EXCLUDE_DIRS as _SOURCE_EXCLUDE_DIRS,
)
from conformance.suite.schema.findings import Finding

from ._common import (
    RULE_D003,
    _DepEntry,
    _make_finding,
    _normalise_name,
    _parse_requirement,
)


def _collect_top_level_imports(py_files: Iterable[Path]) -> set[str]:
    """Return the set of top-level module names imported across *py_files*.

    Only the first dotted component of each import target is kept (``import
    a.b.c`` and ``from a.b import x`` both contribute ``a``).  Relative imports
    (``from . import x``) are skipped — they target the app's own package, never
    a declared third-party distribution.
    """
    return _collect_source_usage(py_files)[0]


# A SQLAlchemy dialect string encodes the DBAPI driver as ``dialect+driver`` —
# in a URL (``mysql+aiomysql://…``) or a ``drivername`` value
# (``URL.create(drivername="mysql+aiomysql", …)``). SQLAlchemy imports that
# driver package at runtime from the string, so it never appears as a Python
# ``import`` — yet the package is genuinely used. Capture the ``driver`` half.
_SQLALCHEMY_DIALECT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+([A-Za-z_][A-Za-z0-9_]+)")


def _collect_dialect_drivers(py_files: Iterable[Path]) -> set[str]:
    """Return DBAPI driver names referenced in SQLAlchemy ``dialect+driver`` strings.

    Scans string literals across *py_files* for the ``dialect+driver`` form and
    keeps the ``driver`` component (``mysql+aiomysql`` -> ``aiomysql``). Used to
    mark a dependency loaded dynamically by SQLAlchemy as used, so D003 does not
    flag it as unimported. Deliberately biased toward matching (WARN-tier, zero
    false positives): an over-captured token only ever suppresses a D003 finding
    for a dependency literally named like that token.
    """
    return _collect_source_usage(py_files)[1]


# A SQLAlchemy URL's scheme also names the dialect class to load. A dialect
# SQLAlchemy does not ship is resolved through the ``sqlalchemy.dialects``
# entry-point group: ``crate://…`` loads the entry point named ``crate`` and
# ``foo+bar://…`` loads ``foo.bar``. The package registering it (for example
# sqlalchemy-cratedb) is therefore used without ever being imported, and a bare
# ``scheme://`` carries no ``+driver`` for _collect_dialect_drivers to see.
_SQLALCHEMY_URL_SCHEME_RE = re.compile(
    r"(?<![A-Za-z0-9_.+-])([A-Za-z_][A-Za-z0-9_]*)(?:\+([A-Za-z_][A-Za-z0-9_]*))?://"
)


# A ``URL.create(drivername=…)`` value names the dialect with no ``://``.
_SQLALCHEMY_DRIVERNAME_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)(?:\+([A-Za-z_][A-Za-z0-9_]*))?$"
)


# SDK classes that create a SQLAlchemy engine on the repo's behalf. A connector
# built on ``BaseSQLClient`` loads its dialect through SQLAlchemy without ever
# importing ``sqlalchemy`` itself. Importing the module is not enough: the SDK
# imports SQLAlchemy lazily, inside ``BaseSQLClient.load()``.
_SQLALCHEMY_LOADER_CLASSES = frozenset({"BaseSQLClient", "AsyncBaseSQLClient"})


# Modules those classes are importable from: the defining module and the
# public package that re-exports them.
_SQLALCHEMY_LOADER_MODULES = frozenset(
    {"application_sdk.clients.sql", "application_sdk.clients"}
)


def _dialect_entry_point_name(dialect: str, driver: str | None) -> str:
    """Render a dialect the way SQLAlchemy looks it up (``foo+bar`` -> ``foo.bar``)."""
    return f"{dialect}.{driver}" if driver else dialect


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Return ``id()`` of every module/class/function docstring constant."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def _url_create_drivername(call: ast.Call) -> str | None:
    """Return the literal ``drivername`` of a ``URL.create(...)`` call, else None.

    Only a direct string constant counts: a name, f-string or concatenation is
    not resolved, so a partial value such as ``f"crate{suffix}"`` can never be
    read as the complete dialect ``crate``.
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "create"):
        return None
    owner = func.value
    owner_name = (
        owner.id
        if isinstance(owner, ast.Name)
        else owner.attr
        if isinstance(owner, ast.Attribute)
        else None
    )
    if owner_name != "URL":
        return None
    value = call.args[0] if call.args else None
    if value is None:
        value = next((kw.value for kw in call.keywords if kw.arg == "drivername"), None)
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _collect_source_usage(
    py_files: Iterable[Path],
) -> tuple[set[str], set[str], set[str]]:
    """Return ``(top-level imports, dialect drivers, dialect entry-point names)``.

    Each source file is read and parsed once.

    Dialect entry-point names come from ``scheme://`` in any string literal
    (f-string literal parts and config-mapping values included) and from a
    literal ``URL.create(drivername=…)``. Like _collect_dialect_drivers this is
    a literal scan, not data-flow analysis, and deliberately biased toward
    matching (WARN-tier): a scheme only ever clears the finding for a
    dependency that registers a ``sqlalchemy.dialects`` entry point under that
    exact name. Two sound exclusions bound it: docstrings are not evidence, and
    no name is credited unless the repo imports ``sqlalchemy`` or references an
    SDK SQL client class (``_SQLALCHEMY_LOADER_CLASSES``) somewhere — without
    SQLAlchemy nothing loads a dialect entry point. The gate is repo-wide
    because a URL constant often lives in a config module that never imports
    SQLAlchemy itself.
    """
    modules: set[str] = set()
    drivers: set[str] = set()
    dialect_names: set[str] = set()
    loads_sqlalchemy = False
    for path in py_files:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            # Parse from bytes so ``ast`` honours a PEP 263 coding cookie; a
            # legacy non-UTF-8 source (e.g. ``# -*- coding: latin-1 -*-``) is
            # decoded correctly instead of crashing on a UTF-8 decode.
            tree = ast.parse(raw)
        except (SyntaxError, ValueError):
            continue
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    modules.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call):
                drivername = _url_create_drivername(node)
                match = (
                    _SQLALCHEMY_DRIVERNAME_RE.fullmatch(drivername)
                    if drivername is not None
                    else None
                )
                if match is not None:
                    dialect_names.add(_dialect_entry_point_name(*match.groups()))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in _SQLALCHEMY_DIALECT_RE.finditer(node.value):
                    drivers.add(match.group(1))
                if id(node) in docstrings:
                    continue
                for match in _SQLALCHEMY_URL_SCHEME_RE.finditer(node.value):
                    dialect_names.add(_dialect_entry_point_name(*match.groups()))
        loads_sqlalchemy = loads_sqlalchemy or _references_sdk_sql_client(tree)
    if "sqlalchemy" not in modules and not loads_sqlalchemy:
        dialect_names = set()
    return modules, drivers, dialect_names


def _references_sdk_sql_client(tree: ast.AST) -> bool:
    """Whether *tree* uses an SDK SQL client class, not merely imports it.

    A use is ``BaseSQLClient``/``AsyncBaseSQLClient`` as a class base or as the
    callee of a call — reached by a name ``from <loader module> import …``
    bound, or by an attribute access such as ``sql.BaseSQLClient`` whose owner
    resolves to a loader module through the file's imports. An import alone,
    an import under ``if TYPE_CHECKING:``, and a type annotation never count,
    because none of them runs ``BaseSQLClient.load()``, the only place the SDK
    imports SQLAlchemy.
    """
    type_only = _type_checking_ids(tree)
    class_names: set[str] = set()
    bound_modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if id(node) in type_only:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound_modules[alias.asname] = alias.name
                else:
                    top = alias.name.split(".", 1)[0]
                    bound_modules[top] = top
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                if (
                    node.module in _SQLALCHEMY_LOADER_MODULES
                    and alias.name in _SQLALCHEMY_LOADER_CLASSES
                ):
                    class_names.add(local)
                else:
                    bound_modules[local] = f"{node.module}.{alias.name}"
    # A name the file also binds some other way may not be the SDK's class or
    # module where it is used; the scan is file-wide, so drop it outright.
    rebound = _non_import_bindings(tree)
    class_names -= rebound | bound_modules.keys()
    for name in rebound:
        bound_modules.pop(name, None)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            runtime_refs: list[ast.expr] = node.bases
        elif isinstance(node, ast.Call):
            runtime_refs = [node.func]
        else:
            continue
        for ref in runtime_refs:
            if isinstance(ref, ast.Name) and ref.id in class_names:
                return True
            if (
                isinstance(ref, ast.Attribute)
                and ref.attr in _SQLALCHEMY_LOADER_CLASSES
                and _resolve_dotted(ref.value, bound_modules)
                in _SQLALCHEMY_LOADER_MODULES
            ):
                return True
    return False


def _non_import_bindings(tree: ast.AST) -> set[str]:
    """Return every name *tree* binds other than by an import statement."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _type_checking_ids(tree: ast.AST) -> set[int]:
    """Return ``id()`` of every node inside an ``if TYPE_CHECKING:`` body."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        name = (
            test.id
            if isinstance(test, ast.Name)
            else test.attr
            if isinstance(test, ast.Attribute)
            else None
        )
        if name != "TYPE_CHECKING":
            continue
        for stmt in node.body:
            ids.update(id(child) for child in ast.walk(stmt))
    return ids


def _resolve_dotted(node: ast.expr, bound_modules: dict[str, str]) -> str | None:
    """Resolve ``a.b.c`` to a dotted module path through the file's imports."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in bound_modules:
        return None
    return ".".join([bound_modules[node.id], *reversed(parts)])


def _collect_dialect_names(py_files: Iterable[Path]) -> set[str]:
    """Return dialect entry-point names selected by SQLAlchemy URL schemes."""
    return _collect_source_usage(py_files)[2]


def _scan_unused_dependencies(
    dep_entries: list[_DepEntry],
    imported_modules: set[str],
    suppressions: SuppressionsMap,
    file: str,
    *,
    dist_import_map: Mapping[str, set[str] | None],
    dialect_drivers: set[str],
    dialect_names: set[str],
    dialect_entry_points: Mapping[str, set[str]],
) -> tuple[list[Finding], list[str]]:
    """Return (D003 findings, names of dependencies skipped as unresolvable).

    A dependency is flagged when the import names it provides are all absent
    from *imported_modules* AND it is not referenced as a SQLAlchemy
    ``dialect+driver`` (``dialect_drivers``) AND none of the
    ``sqlalchemy.dialects`` entry points it registers
    (``dialect_entry_points``) is selected by a URL scheme in source
    (``dialect_names``).  A dependency whose
    ``dist_import_map`` value is ``None`` (not importable in this environment) is
    skipped and returned in the second list so the caller can surface it — never
    silently dropped.
    """
    findings: list[Finding] = []
    unresolved: list[str] = []

    # The caller filters out the SDK self-dependency when building dep_entries,
    # so every entry here is a third-party package eligible for the unused check.
    for entry in dep_entries:
        provided = dist_import_map.get(entry.name)
        if not provided:
            # None (not installed) or empty (no resolvable import names) -> we
            # cannot prove it is unused, so skip and report rather than flag.
            unresolved.append(entry.name)
            continue
        if provided & imported_modules:
            continue  # at least one provided module is imported -> used
        if entry.name in dialect_drivers or provided & dialect_drivers:
            continue  # loaded dynamically by SQLAlchemy via a dialect+driver string
        if dialect_entry_points.get(entry.name, set()) & dialect_names:
            continue  # its registered SQLAlchemy dialect is selected by a URL scheme
        provided_list = ", ".join(sorted(provided))
        findings.append(
            _make_finding(
                rule_id=RULE_D003,
                file=file,
                line=entry.line,
                column=entry.column,
                message=(
                    f"'{entry.name}' is declared in [project.dependencies] but none "
                    f"of the modules it provides ({provided_list}) are imported "
                    f"anywhere in source. If it is unused, remove it. Otherwise it may "
                    f"be loaded dynamically (importlib), pulled in via an entry "
                    f"point/plugin, or required by a framework/server it is not "
                    f"directly imported by — verify before removing, or annotate the "
                    f"line with '# conformance: ignore[D003] <reason>'."
                ),
                suppressions=suppressions,
            )
        )

    return findings, unresolved


def _constraint_entry_lines(text: str) -> Iterator[tuple[str, int, int]]:
    """Yield ``(raw, line, column)`` for each ``[tool.uv] constraint-dependencies``
    array entry, preserving source positions (tomllib discards them)."""
    table_re = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
    entry_re = re.compile(r"""(["'])([^"']+)\1""")
    section: str | None = None
    in_array = False
    for ln, line in enumerate(text.splitlines(), start=1):
        stripped = line.split("#", 1)[0]
        table = table_re.match(line)
        if table is not None:
            section = table.group(1).strip()
            in_array = False
            continue
        if section != "tool.uv":
            continue
        if not in_array:
            key, sep, rest = stripped.partition("=")
            if sep and key.strip() == "constraint-dependencies":
                in_array = "[" in rest
                stripped = rest.split("[", 1)[-1]
            else:
                continue
        for match in entry_re.finditer(stripped):
            yield match.group(2), ln, match.start(2) + 1
        if "]" in stripped:
            in_array = False


def _scan_constraint_floors(text: str, rel_pyproject: str) -> list[Finding]:
    """D003: app-local ``[tool.uv] constraint-dependencies`` floors.

    Surface each entry for review.  Generic transitive floors belong in the
    SDK's declared dependency ranges, not in app-local direct requirements or
    constraints.  A documented app-specific minimum may be retained with a
    reasoned D003 exception only when the SDK's declared range does not
    guarantee it.  App repos only — the SDK's own pyproject is exempt.
    """
    suppressions = parse_toml_suppressions(text)
    findings: list[Finding] = []
    for raw, line, column in _constraint_entry_lines(text):
        parsed = _parse_requirement(raw)
        name = parsed[0] if parsed else raw
        findings.append(
            _make_finding(
                rule_id=RULE_D003,
                file=rel_pyproject,
                line=line,
                column=column,
                message=(
                    f"'{name}' is floored in [tool.uv] constraint-dependencies "
                    f"('{raw}'). This finding surfaces app-local floors for review: "
                    f"generic transitive floors belong in atlan-application-sdk's "
                    f"declared dependency ranges and should be removed from the app, "
                    f"not relocated between a direct dependency and a constraint. "
                    f"A documented app-specific minimum may be retained only when "
                    f"the SDK's declared range does not guarantee it, with a reasoned "
                    f"D003 exception naming the app floor and SDK range gap. A resolved "
                    f"lockfile version alone is not a future guarantee."
                ),
                suppressions=suppressions,
            )
        )
    return findings


_TEST_DIRS: frozenset[str] = frozenset({"test", "tests"})


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(root).parts
    except ValueError:
        return path.parts


def _is_test_source(path: Path, root: Path) -> bool:
    """Whether *path* is a test-only Python source (D004's import context)."""
    parts = _relative_parts(path, root)
    return (
        any(part in _TEST_DIRS for part in parts[:-1])
        or path.name == "conftest.py"
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def _is_app_source(path: Path, root: Path) -> bool:
    """Whether *path* is in the shared (non-test) AST walk's scope.

    Mirrors :func:`_discover_sources`, so D003/D010 read the same files they
    did before :func:`discover` started also returning test sources for D004.
    """
    parts = _relative_parts(path, root)
    return not (
        any(part in _TEST_DIRS for part in parts[:-1])
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def _discover_test_sources(root: Path) -> list[Path]:
    """Find test-only Python sources without changing shared source discovery.

    The common AST walk intentionally excludes tests. D004 needs their imports
    to tell an app-owned test dependency from a redundant SDK tooling pin, so it
    makes a separate, test-scoped walk here. Returned through :func:`discover`
    so the runner's ``--exclude`` prefixes apply to these files too.
    """
    excluded_dirs = _SOURCE_EXCLUDE_DIRS - _TEST_DIRS
    paths: list[Path] = []
    for path in root.rglob("*.py"):
        directory_parts = path.relative_to(root).parts[:-1]
        if not _is_test_source(path, root):
            continue
        if set(directory_parts) & excluded_dirs:
            continue
        if any(part.startswith(".") for part in directory_parts):
            continue
        paths.append(path)
    return sorted(paths)


def _owning_pyproject(path: Path, pyprojects: list[Path]) -> Path | None:
    """Return the nearest ``pyproject.toml`` whose directory contains *path*."""
    owner: Path | None = None
    for pyproject in pyprojects:
        if not path.is_relative_to(pyproject.parent):
            continue
        if owner is None or len(pyproject.parent.parts) > len(owner.parent.parts):
            owner = pyproject
    return owner


def _test_imported_distributions(test_sources: list[Path]) -> set[str]:
    """Return uniquely-owned distributions directly imported by *test_sources*."""
    if not test_sources:
        return set()
    imported_modules, _, _ = _collect_source_usage(test_sources)
    if not imported_modules:
        return set()

    distributions_by_module = importlib_metadata.packages_distributions()
    direct: set[str] = set()
    for module in imported_modules:
        owners = {
            _normalise_name(name) for name in distributions_by_module.get(module, ())
        }
        # A shared namespace (e.g. ``google``) does not prove which individual
        # distribution the test uses, so only credit an unambiguous owner.
        if len(owners) == 1:
            direct.update(owners)
    return direct
