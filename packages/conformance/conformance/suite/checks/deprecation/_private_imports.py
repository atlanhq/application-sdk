"""B008 ``PrivateSdkModuleImport`` — flag an app reaching into SDK internals.

Runs against *consumer apps* (scope ``app``). Flags any import that traverses an
underscore-prefixed component of an ``application_sdk`` module path, or that
binds an underscore-prefixed name out of an SDK module:

* ``from application_sdk.execution._temporal.preflight_gate import X``
* ``import application_sdk.execution._temporal.worker``
* ``from application_sdk.execution._temporal import preflight_gate``
* ``from application_sdk.app.base import _helper``

Why this rule exists
--------------------

FND-2388. SDK 3.36.0 reshaped ``application_sdk/execution/_temporal/preflight_gate.py``
and fifteen connector repos stopped collecting tests. Nine of the removed names
were involved; the two with the widest blast radius were
``_GATE_BROKEN_CATEGORIES`` (nine repos) and ``_is_gate_broken`` (two) — both
underscore-private, both imported from app *test suites*.

Nothing had told those apps they were doing anything unusual. The
``adopt-preflight-gate`` skill never instructed it; the pattern almost certainly
propagated by copying the SDK's own tests, which quite reasonably import the
internals of the module they test. Python offers no enforcement here at all: a
leading underscore is a convention with no runtime meaning, and
``from pkg._private import thing`` works exactly as well as any other import.
This rule is that missing enforcement.

The boundary is not new — the SDK already treats ``_``-prefixed paths as private
everywhere it reasons about its own surface. The capability manifest generator
skips them by construction, which is why
``docs/agents/sdk-capabilities.md`` has never carried a single ``preflight_gate``
symbol. The decision recorded on FND-2388 is to enforce that existing boundary
rather than widen it to match what the fleet happened to do.

Relationship to the surface-removal gate
----------------------------------------

The two halves are deliberately complementary, and the split is the whole design.
``.github/scripts/check_symbol_removals.py`` blocks the SDK from deleting a
*public* name without a deprecation cycle, but only *reports* the deletion of an
underscore-private one — freezing the SDK's internals would be a tax on every
refactor. B008 is what makes that split safe: the SDK keeps the right to change
its privates, and apps get told, once, to stop depending on them.

Tier
----

WARN, not BLOCK. Today the fleet *has* these imports — fifteen repos' worth — and
a BLOCK tier would turn a correct diagnosis into a fleet-wide red wall, which is
the failure mode FND-2388 is about in the first place. WARN reports every one,
the remediation loop can act on them, and the tier is worth revisiting once the
count is near zero.

Coverage limit
--------------

Import statements only. A module-qualified reach-through at the *use* site
(``import application_sdk as sdk; sdk.execution._temporal.x``) is not matched —
the same documented limit B001 carries, and biased toward zero false positives.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

from ._manifest import SDK_IMPORT_ROOT

_RULE_ID = "B008"


def _is_sdk_module(name: str) -> bool:
    return name == SDK_IMPORT_ROOT or name.startswith(SDK_IMPORT_ROOT + ".")


def _private_component(dotted: str) -> str | None:
    """The first underscore-prefixed component of *dotted*, if any.

    Dunders are excluded: ``__init__`` and friends are plumbing, not a private
    subpackage, and no app writes them in an import path anyway.
    """
    for part in dotted.split("."):
        if part.startswith("_") and not part.startswith("__"):
            return part
    return None


def _message(path: str, component: str) -> str:
    return (
        f"`{path}` reaches into SDK internals — `{component}` is private. The SDK "
        "changes private modules and names without a deprecation cycle, which is "
        "exactly how a 3.36.0 refactor stopped fifteen connector repos from "
        "collecting tests (FND-2388). Import the public equivalent instead, or "
        "test through the public behaviour rather than the internal helper. If no "
        "public equivalent exists, that is an SDK gap worth raising rather than "
        "routing around."
    )


def scan_private_imports(
    tree: ast.Module,
    file: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Return B008 findings for every private-SDK import in *tree*."""
    findings: list[Finding] = []

    def emit(node: ast.AST, path: str, component: str) -> None:
        findings.append(
            make_finding(
                filename=file,
                rule_id=_RULE_ID,
                node=node,
                message=_message(path, component),
                directives=directives,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_sdk_module(alias.name):
                    continue
                component = _private_component(alias.name)
                if component:
                    emit(node, alias.name, component)
        elif isinstance(node, ast.ImportFrom):
            # A relative import inside an app resolves against the app's own
            # package, never the SDK's — `from ._helpers import x` in app code is
            # the app's business.
            if node.level:
                continue
            module = node.module or ""
            if not _is_sdk_module(module):
                continue
            component = _private_component(module)
            if component:
                emit(node, module, component)
                continue
            # The module path is public, so check what is being pulled out of it:
            # `from application_sdk.app.base import _helper`, and the submodule
            # form `from application_sdk.execution._temporal import x` is already
            # covered above.
            for alias in node.names:
                name_component = _private_component(alias.name)
                if name_component:
                    emit(node, f"{module}.{alias.name}", name_component)

    return findings
