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

    python3 <sdk>/.github/scripts/gen_app_api.py fix     # relative imports, no logging
    python3 <sdk>/.github/scripts/gen_app_api.py check   # CI: config valid, imports relative
    python3 <sdk>/.github/scripts/gen_app_api.py build --out dist-api [--member api]

``check`` is a no-op for an app without ``[tool.atlan-app-api]``. ``build`` with
``--member`` and no config builds that directory as-is (an app that keeps its
handler in a hand-written workspace member).
"""

from __future__ import annotations

import argparse
import ast
import glob
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
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
}


_INIT = '''"""The {name} app's handler as the consolidated API host installs it.

Built by application-sdk gen_app_api.py from [tool.atlan-app-api]; the code
lives in app/ and is edited there.
"""

from __future__ import annotations

from .{module} import {cls}

#: The instance the host serves (entry point ``atlan.app_api: {name}``).
handler = {cls}()

__all__ = ["{cls}", "handler"]
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

    @property
    def name(self) -> str:  # mysql
        return re.sub(r"^atlan-|-app$", "", self.project)

    @property
    def distribution(self) -> str:  # atlan-mysql-api
        return re.sub(r"-app$", "", self.project) + "-api"

    @property
    def package(self) -> str:  # atlan_mysql_api
        return self.distribution.replace("-", "_")


def load(root: Path) -> Config | None:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
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
    )


def _module_file(root: Path, module: str) -> str | None:
    base = module.replace(".", "/")
    for rel in (f"{base}.py", f"{base}/__init__.py"):
        if (root / rel).is_file():
            return rel
    return None


def _app_imports(
    tree: ast.Module, module: str
) -> list[tuple[ast.ImportFrom | ast.Import, str, bool]]:
    """``(node, imported app module, is_relative)`` for every app-internal import."""
    package = module.rpartition(".")[0]
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
        for node, target, relative in _app_imports(tree, module):
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

        new = re.sub(r"^(\s*)from (app(?:\.\w+)+) import", relative, text, flags=re.M)
        for old, replacement in KNOWN_MOVES.items():
            new = new.replace(old, replacement)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(rel)
    return changed


#: Method names that make a call a logging statement.
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
_CONTEXT_LOG_METHODS = frozenset({"log_debug", "log_info", "log_warning", "log_error"})


def _logger_names(tree: ast.Module) -> set[str]:
    """Names bound to a logger: ``x = get_logger(...)`` / ``logging.getLogger(...)``."""
    names = {"logger", "log", "_logger", "LOGGER"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if called in {"get_logger", "getLogger"}:
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def logging_statements(tree: ast.Module) -> list[ast.stmt]:
    """Every statement in handler code that only logs, binds a logger, or imports one."""
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
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if called in {"get_logger", "getLogger"}:
                found.append(node)
        elif isinstance(node, ast.Import) and any(
            a.name in {"logging", "loguru"} for a in node.names
        ):
            found.append(node)
        elif isinstance(node, ast.ImportFrom) and (
            (node.module or "").split(".")[0] in {"logging", "loguru"}
            or any(a.name == "get_logger" for a in node.names)
            or (node.module or "").startswith("application_sdk.observability")
        ):
            found.append(node)
    return found


def strip_logging(root: Path, files: list[str]) -> list[str]:
    """Delete logging statements from the listed Python files; return those changed.

    A block left empty gets ``pass``. Handler code reports through its return
    value or a typed AppError, and the SDK's routes log the outcome.
    """
    changed = []
    for rel in files:
        if not rel.endswith(".py"):
            continue
        path = root / rel
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=rel)
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
            if isinstance(n, ast.Assign)
            for t in n.targets
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
        path.write_text(new_text, encoding="utf-8")
        changed.append(rel)
    return changed


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
    return f"atlan-application-sdk-api{extras}{config.sdk_spec}"


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
    module = config.handler_module.removeprefix(APP + ".")
    (package / "__init__.py").write_text(
        _INIT.format(name=config.name, module=module, cls=config.handler_class)
    )
    (where / "pyproject.toml").write_text(_pyproject(config))
    return files


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


def check(root: Path, config: Config) -> list[str]:
    _, absolute = closure(root, config)
    return [
        f"{a} — the handler's files must import each other relatively (run `fix`)"
        for a in absolute
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["fix", "check", "build"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=Path("dist-api"))
    parser.add_argument("--member", help="build this directory when there is no config")
    args = parser.parse_args(argv)

    config = load(args.root)
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
        files, _ = closure(args.root, config)
        for rel in fix_absolute_imports(args.root, files):
            print(f"gen_app_api: rewrote imports in {rel}")
        for rel in strip_logging(args.root, files):
            print(f"gen_app_api: removed logging statements from {rel}")
    problems = check(args.root, config)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        files, _ = closure(args.root, config)
        print(f"gen_app_api: {len(files)} app/ files ship as {config.package}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
