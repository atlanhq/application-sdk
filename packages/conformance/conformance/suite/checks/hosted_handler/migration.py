"""P061 HostedHandlerMigration: what still stands between an app and the host.

Four findings, one rule, all handed to the ``api-server-consolidation-migration``
skill:

1. **Not migrated.** A class under ``app/`` subclasses the SDK's ``Handler`` and
   the root ``pyproject.toml`` has no ``[tool.atlan-app-api]`` block.
2. **Old path still present.** ``server/pyproject.toml`` depends on
   ``atlan-server-sdk``: the hand-written handler copy the consolidated host used
   before, to be deleted once the host serves the app's own handler.
3. **Handler not used correctly** (the block is present). The ``handler =``
   entry does not resolve to a ``Handler`` subclass under ``app/``, a ``data``
   glob matches nothing, or the handler's import closure has an absolute
   ``from app.`` import, an import that moved off the host
   (:data:`KNOWN_MOVES`), or an ``APPLICATION_NAME`` read.
4. **Cannot be hosted yet.** An ``app/<entrypoint>/core.py`` defines
   ``compute_manifest``: the host serves static manifests only.

Cases 3 and 4 mirror application-sdk's ``.github/scripts/gen_app_api.py check``
exactly; that script cannot be imported from an app repo, so the logic is
duplicated here and pinned by a parity test in application-sdk
(``.github/scripts/tests/test_hosted_migration_parity.py``). Logging in the
handler module is P060's, not this rule's.

Inline suppression: ``# conformance: ignore[P061] <reason>`` on the reported
line (in ``pyproject.toml`` or a Python file).
"""

from __future__ import annotations

import ast
import glob
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from conformance.suite.checks._ast_common import (
    _parse_directives,
    make_finding,
    safe_read_text,
)
from conformance.suite.schema.findings import Finding

RULE_ID = "P061"
SECTION = "atlan-app-api"
APP = "app"
SKILL = "api-server-consolidation-migration"

#: SDK imports whose usual path loads worker-only code, and the path handler code
#: uses instead. Kept identical to ``gen_app_api.KNOWN_MOVES`` (parity test).
KNOWN_MOVES = {
    "from application_sdk.execution.heartbeat import run_in_thread": (
        "from application_sdk.common.concurrency import run_in_thread"
    ),
    "from application_sdk.observability.logger_adaptor import get_logger": (
        "from application_sdk.common.logger import get_logger"
    ),
}

_APPLICATION_NAME = re.compile(r"\b(?:ATLAN_)?APPLICATION_NAME\b")
_COMPUTE_MANIFEST = re.compile(r"^\s*(async\s+)?def\s+compute_manifest\b", re.M)
_SERVER_SDK = re.compile(r"atlan-server-sdk")


@dataclass(frozen=True)
class _At:
    """A bare source position for :func:`make_finding`."""

    lineno: int
    col_offset: int = 0


def _finding(root: Path, rel: str, line: int, message: str) -> Finding:
    text = safe_read_text(root / rel) or ""
    return make_finding(
        filename=rel,
        rule_id=RULE_ID,
        node=_At(line),
        message=f"{message} Follow the `{SKILL}` skill.",
        directives=_parse_directives(text),
    )


def _line_of(text: str, needle: str | re.Pattern[str]) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if needle.search(line) if isinstance(needle, re.Pattern) else needle in line:
            return number
    return 1


def _module_file(root: Path, module: str) -> str | None:
    base = module.replace(".", "/")
    for rel in (f"{base}.py", f"{base}/__init__.py"):
        if (root / rel).is_file():
            return rel
    return None


def _parse(root: Path, rel: str) -> ast.Module | None:
    text = safe_read_text(root / rel)
    if text is None:
        return None
    try:
        return ast.parse(text, filename=rel)
    except SyntaxError:
        return None


def _sdk_handler_bases(tree: ast.Module) -> set[str]:
    """Names in *tree* bound to an ``application_sdk`` class ending in ``Handler``."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "application_sdk"
        ):
            names.update(
                a.asname or a.name for a in node.names if a.name.endswith("Handler")
            )
    return names


def _subclasses_sdk_handler(cls: ast.ClassDef, bases: set[str]) -> bool:
    for base in cls.bases:
        name = base.id if isinstance(base, ast.Name) else None
        if isinstance(base, ast.Attribute) and base.attr == "Handler":
            return True
        if name in bases:
            return True
    return False


def _handler_classes(root: Path) -> list[tuple[str, ast.ClassDef]]:
    """``(file, class)`` for every SDK-handler subclass under ``app/``."""
    found = []
    for path in sorted((root / APP).rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if "/generated/" in f"/{rel}" or "/tests/" in f"/{rel}":
            continue
        tree = _parse(root, rel)
        if tree is None:
            continue
        bases = _sdk_handler_bases(tree)
        found.extend(
            (rel, node)
            for node in tree.body
            if isinstance(node, ast.ClassDef) and _subclasses_sdk_handler(node, bases)
        )
    return found


def _app_imports(
    tree: ast.Module, module: str, is_package: bool = False
) -> list[tuple[ast.ImportFrom | ast.Import, str, bool]]:
    """``(node, imported app module, is_relative)``; as ``gen_app_api._app_imports``."""
    package = module if is_package else module.rpartition(".")[0]
    found: list[tuple[ast.ImportFrom | ast.Import, str, bool]] = []
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


def _closure(
    root: Path, handler_module: str
) -> tuple[list[str], list[tuple[str, int, str]]]:
    """The handler's ``app/`` Python files, and ``(file, line, target)`` for each
    absolute app import among them; as ``gen_app_api.closure``."""
    files: set[str] = set()
    absolute: set[tuple[str, int, str]] = set()
    stack = [handler_module]
    while stack:
        module = stack.pop()
        rel = _module_file(root, module)
        if rel is None or rel in files:
            continue
        files.add(rel)
        tree = _parse(root, rel)
        if tree is None:
            continue
        for node, target, relative in _app_imports(
            tree, module, is_package=rel.endswith("__init__.py")
        ):
            if _module_file(root, target) is None:
                continue  # a name inside a module
            if not relative:
                absolute.add((rel, node.lineno, target))
            stack.append(target)
    return sorted(files), sorted(absolute)


def compute_manifest_hooks(root: Path) -> list[tuple[str, int]]:
    """``(file, line)`` for each ``app/<entrypoint>/core.py`` defining the hook."""
    hooks = []
    for path in sorted((root / APP).glob("*/core.py")):
        text = safe_read_text(path) or ""
        match = _COMPUTE_MANIFEST.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            hooks.append((path.relative_to(root).as_posix(), line))
    return hooks


def _config(root: Path) -> tuple[dict | None, str]:
    text = safe_read_text(root / "pyproject.toml")
    if text is None:
        return None, ""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None, text
    section = data.get("tool", {}).get(SECTION)
    return (section if isinstance(section, dict) else None), text


def _misused(root: Path, section: dict, text: str) -> list[Finding]:
    """Case 3: the block is present but the handler is not used as the host needs."""
    findings: list[Finding] = []
    handler = section.get("handler")
    module, _, cls = (
        handler.partition(":") if isinstance(handler, str) else ("", "", "")
    )
    handler_line = _line_of(text, re.compile(r"^\s*handler\s*="))
    if not module.startswith(APP + ".") or not cls:
        return [
            _finding(
                root,
                "pyproject.toml",
                handler_line,
                f"[tool.{SECTION}].handler must be 'app.<module>:<Class>'.",
            )
        ]
    rel = _module_file(root, module)
    tree = None if rel is None else _parse(root, rel)
    target = next(
        (
            n
            for n in (tree.body if tree else [])
            if isinstance(n, ast.ClassDef) and n.name == cls
        ),
        None,
    )
    if target is None or not _subclasses_sdk_handler(target, _sdk_handler_bases(tree)):
        findings.append(
            _finding(
                root,
                "pyproject.toml",
                handler_line,
                f"[tool.{SECTION}].handler {handler!r} does not name a class in app/ "
                "that subclasses the SDK's Handler; the host mounts that class.",
            )
        )
        return findings
    for pattern in section.get("data", []) or []:
        if not glob.glob(str(pattern), root_dir=root):
            findings.append(
                _finding(
                    root,
                    "pyproject.toml",
                    _line_of(text, str(pattern)),
                    f"[tool.{SECTION}].data pattern {pattern!r} matches nothing.",
                )
            )
    files, absolute = _closure(root, module)
    findings.extend(
        _finding(
            root,
            file,
            line,
            f"Absolute import of {target_module} in the handler's files; they must "
            "import each other relatively (`gen_app_api.py fix` rewrites it).",
        )
        for file, line, target_module in absolute
    )
    for file in files:
        source = safe_read_text(root / file) or ""
        for old, new in KNOWN_MOVES.items():
            if old in source:
                findings.append(
                    _finding(
                        root,
                        file,
                        _line_of(source, old),
                        f"`{old}` is absent on the host; use `{new}` "
                        "(`gen_app_api.py fix` rewrites it).",
                    )
                )
        for number, line in enumerate(source.splitlines(), 1):
            if _APPLICATION_NAME.search(line):
                findings.append(
                    _finding(
                        root,
                        file,
                        number,
                        "APPLICATION_NAME is the host's own name on the consolidated "
                        "host; use `self.context.app_name`.",
                    )
                )
    return findings


def scan_repo(root: Path) -> list[Finding]:
    """Every P061 finding for the app at *root* (empty for a non-app repo)."""
    if not (root / APP).is_dir():
        return []
    section, text = _config(root)
    handlers = _handler_classes(root)
    findings: list[Finding] = []
    if section is None and handlers:
        rel, cls = handlers[0]
        findings.append(
            _finding(
                root,
                rel,
                cls.lineno,
                f"{cls.name} is not served from the consolidated API host yet: add a "
                f"[tool.{SECTION}] block to pyproject.toml naming it.",
            )
        )
    if section is not None:
        findings.extend(_misused(root, section, text))
    server = safe_read_text(root / "server" / "pyproject.toml")
    if server is not None and _SERVER_SDK.search(server):
        findings.append(
            _finding(
                root,
                "server/pyproject.toml",
                _line_of(server, _SERVER_SDK),
                "server/ is a hand-written handler copy on atlan-server-sdk. Serve the "
                "app's own handler instead, then delete server/ once the host's pin "
                "has moved to the new package.",
            )
        )
    if section is not None or handlers:
        findings.extend(
            _finding(
                root,
                file,
                line,
                "compute_manifest runs app code per request; the consolidated host "
                "serves static manifests only, so this app cannot be hosted until the "
                "hook becomes static data.",
            )
            for file, line in compute_manifest_hooks(root)
        )
    findings.sort(key=lambda f: (f.file, f.line, f.column))
    return findings
