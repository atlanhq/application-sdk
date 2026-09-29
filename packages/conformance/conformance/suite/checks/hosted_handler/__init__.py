"""P-series hosted-handler check (P054 HostedHandlerLogs).

Scans the handler code of an app that declares ``[tool.atlan-app-api]`` in its
root ``pyproject.toml``: the module ``handler = "app.<module>:<Class>"`` names,
plus every ``app/`` module it imports (relatively or absolutely), which is what
the consolidated API host installs. Flags logging there. Silent for an app
without the block.

Inline suppression: ``# conformance: ignore[P054] <reason>``.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    discover,
    make_cli_main,
    make_finding,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

SERIES = "P"
RULE_ID = "P054"
SECTION = "atlan-app-api"
APP = "app"

_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
_CONTEXT_LOG_METHODS = frozenset({"log_debug", "log_info", "log_warning", "log_error"})
_DEFAULT_LOGGER_NAMES = frozenset({"logger", "log", "_logger", "LOGGER"})

__all__ = [
    "SERIES",
    "discover",
    "handler_files",
    "main",
    "scan_all",
    "scan_path",
    "scan_text",
]


def _handler_module(root: Path) -> str | None:
    text = safe_read_text(root / "pyproject.toml")
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    handler = data.get("tool", {}).get(SECTION, {}).get("handler")
    if not isinstance(handler, str):
        return None
    module = handler.partition(":")[0]
    return module if module.startswith(APP + ".") else None


def _module_file(root: Path, module: str) -> Path | None:
    base = root / module.replace(".", "/")
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _app_imports(tree: ast.Module, module: str) -> list[str]:
    package = module.rpartition(".")[0]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, node.module] if node.module else parts)
            elif (node.module or "").split(".")[0] == APP:
                base = node.module or ""
            else:
                continue
            found.append(base)
            found.extend(f"{base}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.split(".")[0] == APP)
    return found


def handler_files(root: Path) -> set[Path]:
    """The hosted handler's files under ``root`` (empty when not hosted)."""
    start = _handler_module(root)
    if start is None:
        return set()
    files: set[Path] = set()
    stack = [start]
    while stack:
        module = stack.pop()
        path = _module_file(root, module)
        if path is None or path in files:
            continue
        files.add(path)
        source = safe_read_text(path)
        if source is None:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        stack.extend(_app_imports(tree, module))
    return files


def _logger_names(tree: ast.AST) -> set[str]:
    names = set(_DEFAULT_LOGGER_NAMES)
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


def _logging_sites(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    loggers = _logger_names(tree)
    sites: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            owner = func.value
            if (
                func.attr in _LOG_METHODS
                and isinstance(owner, ast.Name)
                and owner.id in loggers | {"logging"}
            ):
                sites.append((node, f"`{owner.id}.{func.attr}(...)`"))
            elif func.attr in _CONTEXT_LOG_METHODS:
                sites.append((node, f"`.{func.attr}(...)`"))
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if called in {"get_logger", "getLogger"}:
                sites.append((node, f"a logger bound with `{called}(...)`"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in {"logging", "loguru"}:
                    sites.append((node, f"`import {alias.name}`"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (
                module.split(".")[0] in {"logging", "loguru"}
                or module.startswith("application_sdk.observability")
                or any(a.name == "get_logger" for a in node.names)
            ):
                sites.append((node, f"an import from `{module or '.'}`"))
    return sites


def scan_text(text: str, file: str) -> list[Finding]:
    """P054 findings for one handler file's *text* (the caller decides it is one)."""
    try:
        tree = ast.parse(text, filename=file)
    except SyntaxError:
        return []
    directives = _parse_directives(text)
    findings = [
        make_finding(
            filename=file,
            rule_id=RULE_ID,
            node=node,
            message=(
                f"Hosted handler code logs ({what}). Return the result or raise a "
                "typed AppError; the SDK's routes log every outcome. "
                "`gen_app_api.py fix` removes these statements."
            ),
            directives=directives,
        )
        for node, what in _logging_sites(tree)
    ]
    findings.sort(key=lambda f: (f.line, f.column))
    return findings


def _scan_file(path: Path, root: Path) -> list[Finding]:
    text = safe_read_text(path)
    if text is None:
        return []
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    return scan_text(text, str(rel))


def scan_path(path: Path, root: Path) -> list[Finding]:
    """Scan *path* when it is one of *root*'s hosted handler files."""
    if path.resolve() not in {p.resolve() for p in handler_files(root)}:
        return []
    return _scan_file(path, root)


def scan_all(paths: list[Path], root: Path) -> list[Finding]:
    """Scan the discovered files that are *root*'s hosted handler files."""
    hosted = {p.resolve() for p in handler_files(root)}
    if not hosted:
        return []
    return [
        f for path in paths if path.resolve() in hosted for f in _scan_file(path, root)
    ]


main = make_cli_main(
    scan_all=scan_all,
    description=(
        "Hosted-handler P-series check (P054): flag logging in the handler code of "
        "an app that declares [tool.atlan-app-api] (silent otherwise)."
    ),
)


if __name__ == "__main__":
    sys.exit(main())
