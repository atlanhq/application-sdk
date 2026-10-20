#!/usr/bin/env python3
"""Build an app's handler wheel for the consolidated API host, from its app/ code.

The handler, and everything it imports from ``app/``, stays in ``app/`` and is
edited there. The host installs a small wheel that ships those same files under
a unique package name (``atlan_mysql_api``), on ``atlan-application-sdk-api``
instead of the worker SDK. That wheel is **built on demand** from a few lines in
the app's root ``pyproject.toml``; the app commits no packaging::

    [tool.atlan-app-api]
    handler = "app.handler:MySQLAppHandler"   # the Handler class the host serves
    data = ["app/sql/*.sql"]                  # non-Python files the handler reads
    dependencies = ["aiomysql>=0.3.0"]        # the handler's own third-party deps
    extras = ["sql", "aws"]                   # atlan-application-sdk-api extras

``build`` stages the handler module's closure inside ``app/`` (plus ``data``)
as ``<package>/*`` with a generated ``pyproject.toml`` (deps, ``atlan.app_api``
entry point) and ``__init__.py`` (the handler instance), then builds the wheel.

The listed files must import each other **relatively** (``from .client import
SQLClient``): the same file is ``app.handler`` in the worker and
``<package>.handler`` on the host, and only a relative import works under both.

Usage, from the app repo root::

    python3 <sdk>/.github/scripts/gen_app_api.py fix     # relative imports, host-safe paths
    python3 <sdk>/.github/scripts/gen_app_api.py check   # CI: config valid, imports relative

``fix`` deletes logging only from the handler module itself (P060). The other
listed files also run on the worker, so their logging stays; only their logger
import moves to a path the host has (:data:`KNOWN_MOVES`). It refuses, changing
nothing, where a deletion would be unsafe: a log call sharing a line with other
code, a ``get_logger`` import mixed with other names, a logger stored on an
attribute, or an ``except`` block that would be left as ``pass``. It prints
each deletion as ``file:line`` and fails on any unused import, unused variable
or undefined name it introduced (ruff F401/F841/F821). Needs Python 3.11+.

``check`` is a no-op for an app without ``[tool.atlan-app-api]``. ``build`` with
``--member`` and no config builds that directory as-is (an app that keeps its
handler in a hand-written workspace member).
"""

from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: say so before anything else fails
    sys.exit(
        "gen_app_api.py needs Python 3.11+ (tomllib); run it with `uv run --python 3.11`"
    )
from dataclasses import dataclass
from pathlib import Path

SECTION = "atlan-app-api"
APP = "app"

#: SDK imports whose usual path loads worker-only code, and the path handler
#: code uses instead (same object). ``--fix`` rewrites them in listed files.
KNOWN_MOVES = {
    "from application_sdk.execution.heartbeat import run_in_thread": (
        "from application_sdk.common.concurrency import run_in_thread"
    ),
    "from application_sdk.observability.logger_adaptor import get_logger": (
        "from application_sdk.common.logger import get_logger"
    ),
}


_INIT = '''"""The {name} app's handler as the consolidated API host installs it.

Built by application-sdk gen_app_api.py from [tool.atlan-app-api]; the code
lives in app/ and is edited there.
"""

from __future__ import annotations

from .{module} import {cls}

#: The instance the host serves (entry point ``atlan.app_api: {name}``).
handler = {cls}()


def get_asgi_app():  # noqa: ANN201 — FastAPI is imported lazily
    """The ASGI app the host mounts (entry point ``atlan.app_server: {name}``)."""
    from application_sdk.handler.asgi import build_asgi_app

    return build_asgi_app(handler, app_name="{name}", app_package=__name__)


__all__ = ["{cls}", "get_asgi_app", "handler"]
'''


@dataclass(frozen=True)
class Config:
    handler_module: str  # e.g. app.handler
    handler_class: str
    data: list[str]
    dependencies: list[str]
    extras: list[str]
    project: str  # root project name, e.g. atlan-mysql-app
    version: str
    sdk_spec: str  # the root's atlan-application-sdk specifier, e.g. >=3.40,<4
    api_source: dict | None  # root [tool.uv.sources] atlan-application-sdk-api
    service: str = ""  # [tool.atlan-app-api].name, when the Service is not <project>
    sdk_locked: str = ""  # the atlan-application-sdk version in the app's uv.lock

    @property
    def name(self) -> str:  # mysql: the app's Service name, which the host routes on
        return self.service or re.sub(r"^atlan-|-app$", "", self.project)

    @property
    def distribution(self) -> str:  # atlan-mysql-api
        return re.sub(r"-app$", "", self.project) + "-api"

    @property
    def package(self) -> str:  # atlan_mysql_api
        return self.distribution.replace("-", "_")


def load(root: Path) -> Config | None:
    """The ``[tool.atlan-app-api]`` config, or ``None`` when the repo has none.

    A repo with no root ``pyproject.toml`` has nothing to host, so it is ``None``
    too: every app's CI runs ``check``, and only an opted-in app may fail it.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    section = data.get("tool", {}).get(SECTION)
    if section is None:
        return None
    module, _, cls = section["handler"].partition(":")
    if not module.startswith(APP + ".") or not cls:
        raise ValueError(f"[tool.{SECTION}].handler must be 'app.<module>:<Class>'")
    project = data["project"]
    sdk_spec = ""
    for dep in project.get("dependencies", []):
        match = re.match(r"atlan-application-sdk(\[[^\]]*\])?\s*(.*)$", dep)
        if match and not dep.startswith("atlan-application-sdk-"):
            sdk_spec = match.group(2).split(";")[0].strip()
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    return Config(
        handler_module=module,
        handler_class=cls,
        data=list(section.get("data", [])),
        dependencies=list(section.get("dependencies", [])),
        extras=list(section.get("extras", [])),
        project=project["name"],
        version=project["version"],
        sdk_spec=sdk_spec,
        api_source=sources.get("atlan-application-sdk-api"),
        service=str(section.get("name", "")),
        sdk_locked=_locked_version(root, "atlan-application-sdk"),
    )


def _module_file(root: Path, module: str) -> str | None:
    base = module.replace(".", "/")
    for rel in (f"{base}.py", f"{base}/__init__.py"):
        if (root / rel).is_file():
            return rel
    return None


def _app_imports(
    tree: ast.Module, module: str, is_package: bool = False
) -> list[tuple[ast.ImportFrom | ast.Import, str, bool]]:
    """``(node, imported app module, is_relative)`` for every app-internal import.

    A relative import resolves against the module's package: the module itself
    when it is a package's ``__init__.py``, its parent otherwise.
    """
    package = module if is_package else module.rpartition(".")[0]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, node.module] if node.module else parts)
                found.append((node, base, True))
                found.extend((node, f"{base}.{a.name}", True) for a in node.names)
            elif (node.module or "").split(".")[0] == APP:
                found.append((node, node.module or "", False))
                found.extend(
                    (node, f"{node.module}.{a.name}", False) for a in node.names
                )
        elif isinstance(node, ast.Import):
            found.extend(
                (node, a.name, False) for a in node.names if a.name.split(".")[0] == APP
            )
    return found


def closure(root: Path, config: Config) -> tuple[list[str], list[str]]:
    """The handler's files inside app/, and the absolute app imports among them."""
    files: set[str] = set()
    absolute: list[str] = []
    stack = [config.handler_module]
    while stack:
        module = stack.pop()
        rel = _module_file(root, module)
        if rel is None or rel in files:
            continue
        files.add(rel)
        tree = ast.parse((root / rel).read_text(encoding="utf-8"), filename=rel)
        for node, target, relative in _app_imports(
            tree, module, is_package=rel.endswith("__init__.py")
        ):
            if _module_file(root, target) is None:
                continue  # a name inside a module
            if not relative:
                absolute.append(f"{rel}:{node.lineno}: absolute import of {target}")
            stack.append(target)
    for pattern in config.data:
        matches = sorted(glob.glob(pattern, root_dir=root))
        if not matches:
            raise ValueError(
                f"[tool.{SECTION}].data pattern {pattern!r} matches nothing"
            )
        files.update(matches)
    return sorted(files), sorted(set(absolute))


def fix_absolute_imports(root: Path, files: list[str]) -> list[str]:
    """Rewrite ``from app.x import y`` in listed files to relative imports, and
    apply :data:`KNOWN_MOVES`."""
    changed = []
    for rel in files:
        if not rel.endswith(".py"):
            continue
        path = root / rel
        text = path.read_text(encoding="utf-8")
        depth = len(Path(rel).parts) - 1  # app/handler.py → 1, app/a/b.py → 2

        def relative(match: re.Match[str]) -> str:
            target = match.group(2).split(".")
            here = list(Path(rel).parts[:-1])
            common = 0
            while (
                common < min(len(here), len(target)) and here[common] == target[common]
            ):
                common += 1
            dots = "." * (depth - common + 1)
            return f"{match.group(1)}from {dots}{'.'.join(target[common:])} import"

        # Only lines where the parser sees an absolute ``from ... import``: the
        # same text inside a string or docstring (a usage example) is left alone.
        starts = {
            node.lineno
            for node in ast.walk(ast.parse(text, filename=rel))
            if isinstance(node, ast.ImportFrom) and node.level == 0
        }
        lines = text.splitlines(keepends=True)
        for number in starts:
            line = re.sub(
                r"^(\s*)from (app(?:\.\w+)+) import", relative, lines[number - 1]
            )
            for old, replacement in KNOWN_MOVES.items():
                line = line.replace(old, replacement)
            lines[number - 1] = line
        new = "".join(lines)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(rel)
    return changed


#: Method names that make a call a logging statement.
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
_CONTEXT_LOG_METHODS = frozenset({"log_debug", "log_info", "log_warning", "log_error"})


_LOGGER_FACTORIES = frozenset({"get_logger", "getLogger"})
# Names a ``from logging import ...`` / ``from loguru import ...`` may bring in
# that are loggers themselves; anything else from those modules is not logging.
_LOGGING_MODULE_LOGGERS = _LOGGER_FACTORIES | {"Logger", "logger"}


def _logger_import_names(node: ast.ImportFrom) -> frozenset[str]:
    """The names *node* may import that count as loggers."""
    if (node.module or "").split(".")[0] in {"logging", "loguru"}:
        return _LOGGING_MODULE_LOGGERS
    return _LOGGER_FACTORIES


def _called(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _binds_logger(node: ast.stmt) -> bool:
    """``x = get_logger(...)`` / ``x: T = getLogger(...)`` (any target)."""
    value = getattr(node, "value", None)
    return (
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(value, ast.Call)
        and _called(value) in _LOGGER_FACTORIES
    )


def _targets(node: ast.stmt) -> list[ast.expr]:
    return list(node.targets) if isinstance(node, ast.Assign) else [node.target]  # type: ignore[attr-defined]


def _logger_names(tree: ast.Module) -> set[str]:
    """Names bound to a logger, plus the conventional ones."""
    names = {"logger", "log", "_logger", "LOGGER"}
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and _binds_logger(node):
            names.update(t.id for t in _targets(node) if isinstance(t, ast.Name))
    return names


def logging_statements(tree: ast.Module) -> list[ast.stmt]:
    """Every statement in handler code that only logs, binds a logger, or imports one.

    Imports count only when every name they bring in is a logger
    (``logging``, ``loguru``, ``get_logger``); :func:`unsafe_logging` refuses
    the mixed ones instead of deleting a name that is still used.
    """
    loggers = _logger_names(tree)
    found: list[ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute):
                owner = func.value
                if (
                    func.attr in _LOG_METHODS
                    and isinstance(owner, ast.Name)
                    and owner.id in loggers | {"logging"}
                ):
                    found.append(node)
                elif func.attr in _CONTEXT_LOG_METHODS:
                    found.append(node)  # self.context.log_info(...)
        elif isinstance(node, ast.stmt) and _binds_logger(node):
            if all(isinstance(t, ast.Name) for t in _targets(node)):
                found.append(node)
        elif isinstance(node, ast.Import) and all(
            a.name.split(".")[0] in {"logging", "loguru"} for a in node.names
        ):
            found.append(node)
        elif isinstance(node, ast.ImportFrom) and all(
            a.name in _logger_import_names(node) for a in node.names
        ):
            found.append(node)
    return found


def unsafe_logging(tree: ast.Module, text: str, rel: str) -> list[str]:
    """Logging ``fix`` will not delete on its own; each needs a decision by hand."""
    lines = text.splitlines()
    doomed = logging_statements(tree)
    doomed_ids = {id(n) for n in doomed}
    problems = []
    for node in doomed:
        before = lines[node.lineno - 1][: node.col_offset]
        after = lines[(node.end_lineno or node.lineno) - 1][node.end_col_offset or 0 :]
        if before.strip() or after.split("#", 1)[0].strip():
            problems.append(
                f"{rel}:{node.lineno}: shares its line with other statements"
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and _binds_logger(node):
            if not all(isinstance(t, ast.Name) for t in _targets(node)):
                problems.append(
                    f"{rel}:{node.lineno}: a logger stored on an attribute "
                    "(e.g. self.logger); remove it and its uses by hand"
                )
        elif isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            loggers = _logger_import_names(node)
            if names & loggers and names - loggers:
                problems.append(
                    f"{rel}:{node.lineno}: imports {sorted(names & loggers)} together "
                    f"with {sorted(names - loggers)}; split the import by hand"
                )
        elif isinstance(node, ast.ExceptHandler):
            stmts = [s for s in node.body if isinstance(s, ast.stmt)]
            if stmts and all(id(s) in doomed_ids for s in stmts):
                problems.append(
                    f"{rel}:{node.lineno}: removing its log leaves `except: pass`, "
                    "which swallows the failure; raise a typed AppError (or return "
                    "a failed result) there, then rerun fix"
                )
    return problems


def strip_logging(root: Path, files: list[str]) -> list[tuple[str, int, str]]:
    """Delete logging statements from the listed Python files.

    Returns ``(file, line, first line of the statement)`` for every deletion.
    Refuses, changing nothing, when :func:`unsafe_logging` finds anything. A
    block left empty (other than an ``except``, which is refused) gets
    ``pass``. Handler code reports through its return value or a typed
    AppError, and the SDK's routes log the outcome.
    """
    plans = []
    problems: list[str] = []
    for rel in files:
        if not rel.endswith(".py"):
            continue
        text = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(text, filename=rel)
        problems += unsafe_logging(tree, text, rel)
        plans.append((rel, text, tree))
    if problems:
        raise ValueError("fix changed nothing:\n  " + "\n  ".join(problems))
    deleted: list[tuple[str, int, str]] = []
    for rel, text, tree in plans:
        doomed = logging_statements(tree)
        if not doomed:
            continue
        doomed_ids = {id(n) for n in doomed}
        lines = text.splitlines(keepends=True)
        replace: dict[int, str] = {}  # first line index -> replacement ("" = delete)
        spans: list[tuple[int, int]] = []
        for parent in ast.walk(tree):
            for field in ("body", "orelse", "finalbody", "handlers"):
                block = getattr(parent, field, None)
                if not isinstance(block, list) or not block:
                    continue
                stmts = [s for s in block if isinstance(s, ast.stmt)]
                if not stmts:
                    continue
                gone = [s for s in stmts if id(s) in doomed_ids]
                for s in gone:
                    spans.append((s.lineno - 1, s.end_lineno or s.lineno))
                if (
                    gone
                    and len(gone) == len(stmts)
                    and not isinstance(parent, ast.Module)
                ):
                    first = gone[0]
                    indent = lines[first.lineno - 1][: first.col_offset]
                    replace[first.lineno - 1] = f"{indent}pass\n"
        deleted += [
            (rel, s.lineno, lines[s.lineno - 1].strip())
            for s in sorted(doomed, key=lambda s: s.lineno)
        ]
        for start, end in sorted(set(spans), reverse=True):
            new = [replace[start]] if start in replace else []
            lines[start:end] = new
        new_text = "".join(lines)
        new_tree = ast.parse(
            new_text, filename=rel
        )  # never write a file that does not parse
        bound = {
            t.id
            for n in doomed
            if isinstance(n, (ast.Assign, ast.AnnAssign))
            for t in _targets(n)
            if isinstance(t, ast.Name)
        }
        left = sorted(
            {
                n.lineno
                for n in ast.walk(new_tree)
                if isinstance(n, ast.Name) and n.id in bound
            }
        )
        if left:
            raise ValueError(
                f"{rel}: a removed logger is still used on line(s) {left}; "
                "remove those uses by hand, then rerun fix"
            )
        (root / rel).write_text(new_text, encoding="utf-8")
    return deleted


def _ruff() -> str | None:
    """The ruff executable on PATH, or ``None``."""
    return shutil.which("ruff")


def ruff_findings(root: Path, files: list[str]) -> set[tuple[str, str]] | None:
    """ruff's unused-import, unused-variable and undefined-name findings in
    *files*, as ``(file, message)`` without line numbers (deletions shift them).
    ``None`` when ruff is not installed."""
    ruff = _ruff()
    targets = [f for f in files if f.endswith(".py")]
    if ruff is None:
        return None
    if not targets:
        return set()
    out = subprocess.run(
        [
            ruff,
            "check",
            "--no-fix",
            "--select",
            "F401,F841,F821",
            "--output-format",
            "concise",
            *targets,
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    found = set()
    for line in out.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4 and parts[1].isdigit():
            found.add((parts[0], parts[3].strip()))
    return found


def _locked_version(root: Path, name: str) -> str:
    """*name*'s version in the app's ``uv.lock``, or ``""`` (no lock, or not from a registry)."""
    lock = root / "uv.lock"
    if not lock.is_file():
        return ""
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    for package in data.get("package", []):
        if package.get("name") == name and "registry" in package.get("source", {}):
            return str(package.get("version", ""))
    return ""


def _floored(spec: str, locked: str) -> str:
    """*spec* with its lower bound raised to the locked version the app tests against.

    The app declares a range for ``atlan-application-sdk`` but locks and tests
    one version; the host must not resolve the api distribution below that.
    Upper bounds and exclusions are kept.
    """
    if not locked:
        return spec
    keep = [c.strip() for c in spec.split(",") if c.strip().startswith(("<", "!="))]
    return ",".join([f">={locked}", *keep])


def _api_requirement(config: Config) -> str:
    extras = f"[{','.join(config.extras)}]" if config.extras else ""
    source = config.api_source
    if source and "git" in source:
        ref = source.get("rev") or source.get("tag") or source.get("branch")
        sub = (
            f"#subdirectory={source['subdirectory']}"
            if source.get("subdirectory")
            else ""
        )
        return f"atlan-application-sdk-api{extras} @ git+{source['git']}@{ref}{sub}"
    return f"atlan-application-sdk-api{extras}{_floored(config.sdk_spec, config.sdk_locked)}"


def _pyproject(config: Config) -> str:
    deps = [_api_requirement(config), *config.dependencies]
    direct = config.api_source is not None and "git" in config.api_source
    return (
        "[project]\n"
        f'name = "{config.distribution}"\n'
        f'version = "{config.version}"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = [\n" + "".join(f'    "{d}",\n' for d in deps) + "]\n\n"
        '[project.entry-points."atlan.app_api"]\n'
        f'{config.name} = "{config.package}:handler"\n\n'
        '[project.entry-points."atlan.app_server"]\n'
        f'{config.name} = "{config.package}:get_asgi_app"\n\n'
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        + (
            "[tool.hatch.metadata]\nallow-direct-references = true\n\n"
            if direct
            else ""
        )
        + "[tool.hatch.build.targets.wheel]\n"
        f'packages = ["{config.package}"]\n'
    )


#: The contract toolkit's output the host serves: setup forms and manifests
#: (``*.json``) and the input contracts (``_input.py``). Everything else in
#: ``app/generated/`` (e2e scaffolding) stays out of the wheel.
GENERATED = "app/generated"


def generated_files(root: Path) -> list[str]:
    """The files of ``app/generated/`` the host's metadata routes read."""
    base = root / GENERATED
    if not base.is_dir():
        return []
    return sorted(
        p.relative_to(root).as_posix()
        for p in base.rglob("*")
        if p.is_file() and (p.suffix == ".json" or p.name == "_input.py")
    )


def compute_manifest_hooks(root: Path) -> list[str]:
    """``app/<entrypoint>/core.py`` files defining ``compute_manifest``.

    The host serves static manifests only: a hook runs app code at request time,
    which the host's api-only install cannot be trusted to import.
    """
    return sorted(
        p.relative_to(root).as_posix()
        for p in (root / APP).glob("*/core.py")
        if re.search(
            r"^\s*(async\s+)?def\s+compute_manifest\b",
            p.read_text(encoding="utf-8"),
            re.M,
        )
    )


def stage(root: Path, config: Config, where: Path) -> list[str]:
    """Write the host package's source tree into ``where``; return the app files."""
    files, absolute = closure(root, config)
    if absolute:
        raise ValueError(
            "absolute app imports in the handler's files: " + "; ".join(absolute)
        )
    package = where / config.package
    package.mkdir(parents=True)
    for rel in files:
        target = package / rel.removeprefix(APP + "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, target)
    for rel in generated_files(root):
        target = package / rel.removeprefix(APP + "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, target)
        if target.name == "_input.py":
            # Importable as <package>.generated[.<entrypoint>]._input, with none
            # of the app's own generated/__init__.py (it may import e2e tooling).
            for parent in target.relative_to(package).parents:
                init = package / parent / "__init__.py"
                if parent != Path(".") and not init.exists():
                    init.write_text("")
    module = config.handler_module.removeprefix(APP + ".")
    (package / "__init__.py").write_text(
        _INIT.format(name=config.name, module=module, cls=config.handler_class)
    )
    (where / "pyproject.toml").write_text(_pyproject(config))
    return files + generated_files(root)


def content_hash(root: Path, config: Config) -> str:
    """sha256 of the package the host would install: every staged file (path and
    bytes, in path order), the generated ``pyproject.toml`` (its dependencies)
    included.

    A release that changes only worker code leaves it unchanged, so the host's
    pin bump can skip it instead of rolling every hosted app for nothing.
    """
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory() as tmp:
        where = Path(tmp) / "src"
        stage(root, config, where)
        for path in sorted(p for p in where.rglob("*") if p.is_file()):
            digest.update(path.relative_to(where).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def build(root: Path, out: Path, member: str | None = None) -> Path:
    """Build the host wheel into ``out`` and return its path."""
    out.mkdir(parents=True, exist_ok=True)
    config = load(root)
    with tempfile.TemporaryDirectory() as tmp:
        if config is not None:
            source = Path(tmp) / "src"
            stage(root, config, source)
        elif member:
            source = root / member
        else:
            raise ValueError(f"no [tool.{SECTION}] config and no --member to build")
        before = set(out.glob("*.whl"))
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out.resolve()), str(source)],
            check=True,
        )
    (wheel,) = set(out.glob("*.whl")) - before
    return wheel


#: A read of the process-wide app name: ``application_sdk.constants``'s
#: ``APPLICATION_NAME`` or the ``ATLAN_APPLICATION_NAME`` env var it comes from.
_APPLICATION_NAME = re.compile(r"\b(?:ATLAN_)?APPLICATION_NAME\b")


def handler_file(root: Path, config: Config) -> str:
    """The handler module's own file: the only one ``fix`` strips logging from."""
    rel = _module_file(root, config.handler_module)
    if rel is None:
        raise ValueError(
            f"[tool.{SECTION}].handler module {config.handler_module} not found"
        )
    return rel


def check(root: Path, config: Config) -> list[str]:
    files, absolute = closure(root, config)
    problems = [
        f"{a} — the handler's files must import each other relatively (run `fix`)"
        for a in absolute
    ]
    for rel in files:
        if not rel.endswith(".py"):
            continue
        text = (root / rel).read_text(encoding="utf-8")
        problems.extend(
            f"{rel}: `{old}` is absent on the host (run `fix`)"
            for old in KNOWN_MOVES
            if old in text
        )
        problems.extend(
            f"{rel}:{n}: APPLICATION_NAME is the host's own name on the "
            "consolidated host; use `self.context.app_name`"
            for n, line in enumerate(text.splitlines(), 1)
            if _APPLICATION_NAME.search(line)
        )
    handler = _module_file(root, config.handler_module)
    if handler:
        text = (root / handler).read_text(encoding="utf-8")
        problems.extend(
            f"{handler}:{n.lineno}: logs in the hosted handler module (P060); "
            "run `fix`, or report through the result or a typed AppError"
            for n in logging_statements(ast.parse(text, filename=handler))
        )
    problems.extend(
        f"{hook}: compute_manifest runs app code per request; the consolidated "
        "host serves static manifests only, so this app cannot be hosted yet"
        for hook in compute_manifest_hooks(root)
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["fix", "check", "build", "stage", "hash"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=Path("dist-api"))
    parser.add_argument("--member", help="build this directory when there is no config")
    args = parser.parse_args(argv)

    config = load(args.root)
    if args.command == "hash":
        if config is None:
            print(f"::error::no [tool.{SECTION}] config to hash")
            return 1
        print(content_hash(args.root, config))
        return 0
    if args.command == "stage":
        if config is None:
            print(f"::error::no [tool.{SECTION}] config to stage")
            return 1
        try:
            stage(args.root, config, args.out)
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        print(f"gen_app_api: staged {config.package} in {args.out}")
        return 0
    if args.command == "build":
        try:
            print(f"gen_app_api: built {build(args.root, args.out, args.member)}")
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        return 0
    if config is None:
        print(
            f"gen_app_api: no [tool.{SECTION}] in pyproject.toml; nothing to {args.command}"
        )
        return 0
    if args.command == "fix":
        if _ruff() is None:
            print(
                "::error::fix needs ruff on PATH to prove its edits leave no unused "
                "import, unused variable or undefined name; install it "
                "(`uv tool install ruff`) and rerun. Nothing was changed."
            )
            return 1
        handler = handler_file(args.root, config)
        before_files, _ = closure(args.root, config)
        before = ruff_findings(args.root, before_files) or set()
        # Everything fix may write, so a failed run leaves the tree as it found it.
        originals = {
            rel: (args.root / rel).read_text(encoding="utf-8")
            for rel in {*before_files, handler}
            if rel.endswith(".py")
        }

        def restore() -> None:
            for rel, text in originals.items():
                (args.root / rel).write_text(text, encoding="utf-8")

        try:
            deleted = strip_logging(args.root, [handler])
            files, _ = closure(args.root, config)
            rewritten = fix_absolute_imports(args.root, files)
            after = ruff_findings(args.root, files) or set()
        except ValueError as exc:
            restore()
            print(f"::error::{exc}")
            return 1
        introduced = sorted(after - before)
        if introduced:
            restore()
            for rel, message in introduced:
                print(f"::error::fix would leave {rel}: {message}")
            print(
                "::error::fix changed nothing: edit those lines by hand so the "
                "removal is clean, then rerun"
            )
            return 1
        for rel, line, code in deleted:
            print(f"gen_app_api: removed {rel}:{line}: {code}")
        for rel in rewritten:
            print(f"gen_app_api: rewrote imports in {rel}")
    problems = check(args.root, config)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        files, _ = closure(args.root, config)
        print(f"gen_app_api: {len(files)} app/ files ship as {config.package}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
