"""B009 ``DeprecatedHandlerImportPath`` — the handler surface moved to ``application_sdk_api``.

``application_sdk.handler`` and its ``base`` / ``contracts`` / ``context`` /
``manifest`` / ``service_errors`` submodules are shims over
``application_sdk_api.handler*``: every name resolves to the same object, with a
``DeprecationWarning``, until removal in v4.0.  Each shim also re-exports a few
worker-surface names that are *not* deprecated on that path; this module mirrors
those per-module sets exactly, so they are never flagged.

This rule is about the *spelling* of the import, so it reads raw module names and
deliberately does not use ``_ast_common.canonical_sdk_module`` (which would make
the two roots indistinguishable).

Shapes matched:

* ``from application_sdk.handler[.sub] import A, B`` — flagged when any name is
  not a worker-surface name of that module (``*`` always is);
* ``from application_sdk import handler`` / ``from application_sdk.handler import
  contracts`` — a deprecated *module* bound to a local name;
* ``import application_sdk.handler.contracts [as hc]``.

A bound module is flagged unless every ``<binding>.Name`` the file reads is a
worker-surface name of that module — a file that only reads
``hc.PreflightGateMode`` is compliant.  A binding the file never dereferences is
flagged: nothing shows which names it is for.

**Gated on the lock.**  On an SDK that predates the move, ``application_sdk.handler``
is the real module and ``application_sdk_api`` is not installed, so the rewrite
would not import.  When the repo's ``uv.lock`` is readable and does not resolve
``atlan-application-sdk-api``, the rule is not evaluated (see
:func:`api_package_resolvable`); the fix there is an SDK bump, not an edit.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_RULE_ID = "B009"

_OLD_ROOT = "application_sdk.handler"
_NEW_ROOT = "application_sdk_api.handler"

_WORKER_CONTRACTS: frozenset[str] = frozenset(
    {
        "PreflightGateMode",
        "EventTriggerConfig",
        "EventFilterRule",
        "SubscriptionConfig",
        "CloudEventEnvelope",
        "FileUploadResponse",
    }
)

#: Deprecated shim module → the names it re-exports WITHOUT deprecation.  Mirrors
#: each shim's ``_NOT_DEPRECATED`` set in ``application_sdk/handler/*.py``.
DEPRECATED_HANDLER_MODULES: dict[str, frozenset[str]] = {
    "application_sdk.handler": _WORKER_CONTRACTS
    | {"create_app_handler_service", "run_app_handler_service"},
    "application_sdk.handler.base": frozenset(),
    "application_sdk.handler.contracts": _WORKER_CONTRACTS,
    "application_sdk.handler.context": frozenset({"bind_invocation_context"}),
    "application_sdk.handler.manifest": frozenset(),
    "application_sdk.handler.service_errors": frozenset(),
}

#: Submodules of the handler package that are worker modules, not shims:
#: ``from application_sdk.handler import service`` is not a deprecated import.
_WORKER_SUBMODULES: frozenset[str] = frozenset({"service", "invocation"})


API_DISTRIBUTION = "atlan-application-sdk-api"


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def api_package_resolvable(root: Path) -> bool:
    """Whether the app can import ``application_sdk_api`` — i.e. B009 applies.

    ``False`` only when ``root/uv.lock`` is readable and resolves no
    ``atlan-application-sdk-api``: the locked SDK predates the move, so
    ``application_sdk.handler`` is still the real module there.  An absent or
    unparseable lock says nothing either way and keeps the rule evaluated.
    """
    lock = root / "uv.lock"
    if not lock.is_file():
        return True
    try:
        doc = tomllib.loads(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return True
    target = _normalise(API_DISTRIBUTION)
    return any(
        isinstance(pkg, dict) and _normalise(str(pkg.get("name", ""))) == target
        for pkg in doc.get("package", [])
    )


def new_module_path(module: str) -> str:
    """``application_sdk.handler[.sub]`` → ``application_sdk_api.handler[.sub]``."""
    return _NEW_ROOT + module[len(_OLD_ROOT) :]


def _deprecated_names(module: str, names: list[str]) -> list[str]:
    exempt = DEPRECATED_HANDLER_MODULES[module] | (
        _WORKER_SUBMODULES if module == _OLD_ROOT else frozenset()
    )
    return [n for n in names if n not in exempt]


def _attribute_reads(tree: ast.AST, dotted: str) -> tuple[set[str], bool]:
    """Names read as ``<dotted>.Name`` in *tree*, and whether *dotted* is used bare.

    *dotted* is the local spelling of the bound module (``hc`` or
    ``application_sdk.handler.contracts``).  A use that is not an attribute read
    (``f(hc)``) cannot be resolved to names, so it is reported as bare.
    """
    reads: set[str] = set()
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    bare = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        if isinstance(node, ast.Name) and node.id != dotted:
            continue
        if isinstance(node, ast.Attribute):
            try:
                if ast.unparse(node) != dotted:
                    continue
            except (ValueError, TypeError):  # pragma: no cover — malformed node
                continue
        parent = parents.get(id(node))
        if isinstance(parent, ast.Attribute) and parent.value is node:
            reads.add(parent.attr)
        elif isinstance(parent, ast.Attribute):
            continue
        else:
            bare = True
    return reads, bare


def _module_binding_finding(
    tree: ast.AST,
    node: ast.stmt,
    module: str,
    local: str,
    file: str,
    directives: dict[int, _IgnoreDirective],
) -> Finding | None:
    reads, bare = _attribute_reads(tree, local)
    deprecated = _deprecated_names(module, sorted(reads))
    if reads and not bare and not deprecated:
        return None
    used = f" (reads {', '.join(repr(n) for n in deprecated)})" if deprecated else ""
    return make_finding(
        filename=file,
        rule_id=_RULE_ID,
        node=node,
        message=(
            f"Binds the deprecated handler module '{module}'{used}. The handler "
            f"surface lives in '{new_module_path(module)}'; the "
            f"'{_OLD_ROOT}' path is a shim removed in v4.0. Import "
            f"'{new_module_path(module)}' instead — the names and objects are "
            f"unchanged. Suppress with '# conformance: ignore[{_RULE_ID}] <reason>'."
        ),
        directives=directives,
    )


def scan_handler_import_path(
    tree: ast.Module,
    file: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Return B009 findings for every deprecated handler-surface import in *tree*."""
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or not node.module:
                continue
            module = node.module
            if module in DEPRECATED_HANDLER_MODULES:
                names = [a.name for a in node.names]
                submodules = {
                    a.name: a.asname or a.name
                    for a in node.names
                    if f"{module}.{a.name}" in DEPRECATED_HANDLER_MODULES
                }
                deprecated = [
                    n for n in _deprecated_names(module, names) if n not in submodules
                ]
                for name, local in submodules.items():
                    finding = _module_binding_finding(
                        tree, node, f"{module}.{name}", local, file, directives
                    )
                    if finding is not None:
                        findings.append(finding)
                if not deprecated:
                    continue
                listed = ", ".join(f"'{n}'" for n in deprecated)
                kept = [n for n in names if n not in deprecated and n not in submodules]
                split = (
                    f" Keep {', '.join(repr(n) for n in kept)} on '{module}' — "
                    "worker-surface names are not deprecated there."
                    if kept
                    else ""
                )
                findings.append(
                    make_finding(
                        filename=file,
                        rule_id=_RULE_ID,
                        node=node,
                        message=(
                            f"Imports {listed} from the deprecated handler path "
                            f"'{module}', a shim removed in v4.0. Import from "
                            f"'{new_module_path(module)}' instead — same names, "
                            f"same objects.{split} Suppress with "
                            f"'# conformance: ignore[{_RULE_ID}] <reason>'."
                        ),
                        directives=directives,
                    )
                )
            elif module == "application_sdk":
                for alias in node.names:
                    if alias.name != "handler":
                        continue
                    finding = _module_binding_finding(
                        tree,
                        node,
                        _OLD_ROOT,
                        alias.asname or alias.name,
                        file,
                        directives,
                    )
                    if finding is not None:
                        findings.append(finding)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in DEPRECATED_HANDLER_MODULES:
                    continue
                finding = _module_binding_finding(
                    tree,
                    node,
                    alias.name,
                    alias.asname or alias.name,
                    file,
                    directives,
                )
                if finding is not None:
                    findings.append(finding)
    return findings
