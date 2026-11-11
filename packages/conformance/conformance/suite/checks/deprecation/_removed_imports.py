"""B009 ``RemovedSdkImport`` — flag an app importing an SDK module that no longer exists.

Runs against *consumer apps* (scope ``app``). Flags any absolute import that
names a module in :data:`REMOVED_SDK_MODULES`, or a module under one:

* ``from application_sdk.test_utils.e2e.base import BaseTest``
* ``import application_sdk.workflows.metadata_extraction.sql``
* ``from application_sdk.test_utils import e2e`` — the removed module named as
  the imported member;
* ``from application_sdk import workflows``.

Why this rule exists
--------------------

FND-4661. Six connector e2e test files still import
``application_sdk.test_utils.e2e``, which the v3.0.0 cut deleted. Every one of
them fails at collection, so the e2e suite those files define never runs, and
nothing reports it: a collection error in a tier that CI does not run on every
PR reads as "no e2e coverage" rather than as a defect.

B001 cannot see this. It reads the deprecated-symbol manifest, and a symbol
leaves that manifest the moment it is deleted at its horizon — exactly when an
app still importing it breaks. B008 cannot either: the module path is public.
So the removed surface needs its own record.

Why a static registry
---------------------

``detect`` runs through ``uvx``, without the app's own SDK installed, so the
rule cannot ask the installed SDK whether a module resolves. It reads the table
below instead. Each entry is a public ``application_sdk`` module deleted in a
released SDK version and absent from the tree today, collapsed to its topmost
removed package, with the first release that no longer shipped it. A
conformance test checks every entry against the SDK tree, so a module that
comes back cannot stay flagged.

What is out of scope
--------------------

* ``importlib.import_module("...")`` and other string-built imports: not an
  import statement, so not matched.
* An import guarded by ``try: ... except ImportError`` (or
  ``ModuleNotFoundError``): that is a deliberate compatibility probe that
  survives the module's absence, not a dead reference.
* Removed *names* inside modules that still exist: those are deprecated before
  removal, and B001 reports them while the deprecation lasts.

Tier
----

WARN, matching B008: the fleet has these imports today, and the remediation is
app-side.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_RULE_ID = "B009"

_SDK_ROOT = "application_sdk"

#: Where the v3.0.0 removals are mapped to their replacements.
_V3_GUIDE = "docs/upgrade-guide-v3.md in atlanhq/application-sdk"


@dataclass(frozen=True)
class RemovedModule:
    """One removed SDK module: the first release without it, and its successor."""

    removed_in: str
    replacement: str | None = None


#: Public SDK modules that no longer exist, keyed by the topmost removed
#: package. ``removed_in`` is the first ``v*`` release whose tree lacks it.
#: Derived from git history (every deleted public ``application_sdk`` module
#: still absent at HEAD); ``tests/test_removed_imports.py`` holds every key
#: against the SDK tree.
REMOVED_SDK_MODULES: dict[str, RemovedModule] = {
    "application_sdk.activities": RemovedModule("3.0.0", "application_sdk.app"),
    "application_sdk.application": RemovedModule("3.0.0", "application_sdk.app"),
    "application_sdk.clients.async_atlan": RemovedModule("1.0.0"),
    "application_sdk.clients.atlan": RemovedModule("3.0.0"),
    "application_sdk.clients.atlan_auth": RemovedModule("3.0.0"),
    "application_sdk.clients.temporal": RemovedModule("3.0.0"),
    "application_sdk.clients.utils": RemovedModule("3.0.0"),
    "application_sdk.clients.workflow": RemovedModule("3.0.0"),
    "application_sdk.common.credential_utils": RemovedModule("1.0.0"),
    "application_sdk.common.dataframe_utils": RemovedModule("2.0.0"),
    "application_sdk.common.exc_utils": RemovedModule("3.12.0"),
    "application_sdk.common.incremental.state.ancestral_merge": RemovedModule("3.0.0"),
    "application_sdk.common.logger_adaptors": RemovedModule("1.0.0"),
    "application_sdk.common.sql_utils": RemovedModule("3.4.0"),
    "application_sdk.decorators": RemovedModule("3.0.0"),
    "application_sdk.docgen": RemovedModule("3.0.0"),
    "application_sdk.events": RemovedModule("2.0.0"),
    "application_sdk.handlers": RemovedModule("3.0.0", "application_sdk.handler"),
    "application_sdk.inputs": RemovedModule("2.0.0"),
    "application_sdk.interceptors.activity_failure_logging": RemovedModule("3.0.0"),
    "application_sdk.interceptors.cleanup": RemovedModule("3.0.0"),
    "application_sdk.interceptors.correlation_context": RemovedModule("3.0.0"),
    "application_sdk.interceptors.events": RemovedModule("3.0.0"),
    "application_sdk.interceptors.lock": RemovedModule("3.0.0"),
    "application_sdk.interceptors.models": RemovedModule("3.0.0"),
    "application_sdk.interceptors.outputs": RemovedModule("3.0.0"),
    "application_sdk.observability.app_vitals": RemovedModule("3.6.0"),
    "application_sdk.observability.error_classifier": RemovedModule("3.6.0"),
    "application_sdk.outputs.iceberg": RemovedModule("2.0.0"),
    "application_sdk.outputs.json": RemovedModule("2.0.0"),
    "application_sdk.outputs.objectstore": RemovedModule("1.0.0"),
    "application_sdk.outputs.secretstore": RemovedModule("1.0.0"),
    "application_sdk.outputs.statestore": RemovedModule("1.0.0"),
    "application_sdk.server.fastapi.middleware.logmiddleware": RemovedModule("3.0.0"),
    "application_sdk.server.fastapi.middleware.metrics": RemovedModule("3.0.0"),
    "application_sdk.server.fastapi.routers.server": RemovedModule("3.0.0"),
    "application_sdk.server.middleware.metrics": RemovedModule("3.6.0"),
    "application_sdk.services": RemovedModule("3.0.0"),
    "application_sdk.test_utils.credentials": RemovedModule("3.0.0"),
    "application_sdk.test_utils.e2e": RemovedModule(
        "3.0.0", "application_sdk.testing.e2e"
    ),
    "application_sdk.test_utils.hypothesis.strategies.application.fastapi": (
        RemovedModule("1.0.0")
    ),
    "application_sdk.test_utils.hypothesis.strategies.handlers": RemovedModule("3.0.0"),
    "application_sdk.test_utils.hypothesis.strategies.inputs": RemovedModule("3.0.0"),
    "application_sdk.test_utils.hypothesis.strategies.outputs": RemovedModule("3.0.0"),
    "application_sdk.test_utils.hypothesis.strategies.server": RemovedModule("3.0.0"),
    "application_sdk.test_utils.hypothesis.strategies.temporal": RemovedModule("3.0.0"),
    "application_sdk.test_utils.scale_data_generator": RemovedModule("3.0.0"),
    "application_sdk.test_utils.workflow_monitoring": RemovedModule("3.0.0"),
    "application_sdk.testing.e2e.deployer": RemovedModule("3.1.0"),
    "application_sdk.testing.e2e.pods": RemovedModule(
        "3.30.0", "application_sdk.testing.harness.cluster"
    ),
    "application_sdk.worker": RemovedModule("3.0.0"),
    "application_sdk.workflows": RemovedModule("3.0.0", "application_sdk.app"),
}

#: Exceptions whose handler makes an import a deliberate probe.
_IMPORT_PROBE_EXCEPTIONS = frozenset({"ImportError", "ModuleNotFoundError"})


def removed_entry(dotted: str) -> tuple[str, RemovedModule] | None:
    """The registry entry *dotted* falls under, if any.

    *dotted* matches an entry when it is the entry or a module beneath it;
    a sibling sharing a prefix (``application_sdk.workflows_x``) does not.
    """
    if not dotted.startswith(_SDK_ROOT):
        return None
    parts = dotted.split(".")
    for end in range(len(parts), 1, -1):
        candidate = ".".join(parts[:end])
        entry = REMOVED_SDK_MODULES.get(candidate)
        if entry is not None:
            return candidate, entry
    return None


#: Built-in exceptions whose handler also catches ``ModuleNotFoundError``.
_CATCHES_IMPORT_ERROR = _IMPORT_PROBE_EXCEPTIONS | {"Exception", "BaseException"}


def _builtin_exception_name(exc: ast.expr) -> str | None:
    """The built-in exception *exc* names, or ``None`` when it may not be one.

    Only a bare name (``ImportError``) or an explicit ``builtins.ImportError``
    counts. Any other attribute (``app_errors.ImportError``) may be an
    application-defined class that does not catch ``ModuleNotFoundError``, so
    it is not read as a probe.
    """
    if isinstance(exc, ast.Name):
        return exc.id
    if (
        isinstance(exc, ast.Attribute)
        and isinstance(exc.value, ast.Name)
        and exc.value.id == "builtins"
    ):
        return exc.attr
    return None


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    """True when *handler* catches ImportError (bare ``except`` included)."""
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_builtin_exception_name(exc) in _CATCHES_IMPORT_ERROR for exc in types)


#: Scopes whose body does not run when the enclosing ``try`` does.
_DEFERRED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _imports_run_by(stmt: ast.stmt) -> list[ast.Import | ast.ImportFrom]:
    """Imports *stmt* executes immediately, not those in a deferred body.

    An import inside a function defined in the ``try`` body runs when the
    function is called, after the ``try`` has ended, so the handler does not
    guard it. A class body runs at definition time and is walked; its methods
    are not.
    """
    found: list[ast.Import | ast.ImportFrom] = []
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.append(node)
            continue
        if isinstance(node, _DEFERRED_SCOPES):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return found


def _probe_guarded_imports(tree: ast.Module) -> set[int]:
    """``id()`` of every import a ``try`` that catches ImportError guards."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try | ast.TryStar):
            continue
        if not any(_handler_catches_import_error(h) for h in node.handlers):
            continue
        for stmt in node.body:
            guarded.update(id(inner) for inner in _imports_run_by(stmt))
    return guarded


def _message(imported: str, root: str, entry: RemovedModule) -> str:
    if entry.replacement is not None:
        fix = f"Import from `{entry.replacement}` instead, or delete the dead code."
    elif entry.removed_in == "3.0.0":
        fix = (
            "Move to its v3 replacement (mapped in "
            f"{_V3_GUIDE}), or delete the dead code."
        )
    else:
        fix = "Move to the SDK's current equivalent, or delete the dead code."
    return (
        f"`{imported}` no longer exists: the SDK removed `{root}` in "
        f"v{entry.removed_in}. Any file importing it fails at import, so a test "
        "file that does never collects and its tests never run, with no failure "
        f"anyone reads. {fix}"
    )


def scan_removed_imports(
    tree: ast.Module,
    file: str,
    directives: dict[int, _IgnoreDirective],
    own_roots: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Return B009 findings for every import of a removed SDK module in *tree*."""
    if _SDK_ROOT in own_roots:
        # The SDK itself, or a repo vendoring it: its tree is the authority.
        return []
    guarded = _probe_guarded_imports(tree)
    findings: list[Finding] = []

    def emit(node: ast.stmt, dotted: str) -> bool:
        hit = removed_entry(dotted)
        if hit is None:
            return False
        root, entry = hit
        findings.append(
            make_finding(
                filename=file,
                rule_id=_RULE_ID,
                node=node,
                message=_message(dotted, root, entry),
                directives=directives,
            )
        )
        return True

    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                emit(node, alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            if emit(node, node.module):
                continue
            # `from application_sdk.test_utils import e2e`: the parent exists,
            # the member is the removed module. One finding per statement.
            for alias in node.names:
                if emit(node, f"{node.module}.{alias.name}"):
                    break
    return findings
