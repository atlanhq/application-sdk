"""B008 ``PrivateModuleImport`` — flag an app reaching into someone else's internals.

Runs against *consumer apps* (scope ``app``). Flags any import or attribute use
that reaches a ``_``-prefixed module or name belonging to code the app does not
own:

* ``from application_sdk.execution._temporal.preflight_gate import X``
* ``import application_sdk.execution._temporal.worker``
* ``from application_sdk.execution._temporal import preflight_gate``
* ``from application_sdk.app.base import _helper``
* ``from pandas._libs import x`` / ``from temporalio.api._grpc import y``
* ``import application_sdk as sdk`` … ``sdk.execution._temporal.thing``

The app's **own** privates are never flagged. A relative import
(``from ._helpers import x``) is own code by construction, and an absolute
import rooted at one of the repo's own top-level packages (``app``, ``tests``,
``local``, …) is too. An app is free to organise its own internals however it
likes; what it cannot do is depend on somebody else's.

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

Why it is not SDK-specific
--------------------------

The SDK is where it bit us, but nothing about the failure is particular to the
SDK: it is the general hazard of depending on a boundary the owner never
promised to keep. ``pandas._libs``, ``temporalio.api._grpc`` and
``pydantic._internal`` are all exactly as free to change under an app as
``application_sdk._temporal`` was, and an app that imports one has the same
latent breakage with the same absence of warning. An app sits at the leaf of the
dependency chain — everything it imports is somebody else's — so the rule is
"nothing foreign and private", not "nothing of the SDK's".

Relationship to the surface-removal gate
----------------------------------------

The two halves are complementary, and the split is the design. The SDK's own
``.github/scripts/check_symbol_removals.py`` blocks it from deleting a *public*
name without a deprecation cycle, but only *reports* the deletion of an
underscore-private one — freezing the SDK's internals would be a tax on every
refactor. B008 is what makes that split safe: publishers keep the right to
change their privates, and apps get told, once, to stop depending on them.

That asymmetry is also why the manifest-and-deprecation machinery stays on the
publisher's side and this rule is the only app-side piece. Deprecation is a
promise made by whoever owns the surface; an app owns none, so it has nothing to
deprecate and no manifest to keep.

Tier
----

WARN, not BLOCK. Today the fleet *has* these imports — fifteen repos' worth from
the SDK alone — and a BLOCK tier would turn a correct diagnosis into a
fleet-wide red wall, which is the failure mode FND-2388 is about in the first
place. WARN reports every one, the remediation loop can act on them, and the
tier is worth revisiting once the count is near zero.
"""

from __future__ import annotations

import ast
from pathlib import Path

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_RULE_ID = "B008"

#: Directories that are never an import root of the repo's own code.
_NOT_IMPORT_ROOTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


def own_import_roots(root: Path) -> frozenset[str]:
    """Every module/package name that exists anywhere in *root*'s own tree.

    The question this answers is "does this import originate outside the app's
    own code?", and the only reliable signal for that is whether a module of
    that name is *in the repo*. An app's own package is not reliably at the top
    of it, and is imported absolutely from wherever it does live. Measured
    against the fleet, a shallower sweep called all of this foreign:

    * ``src/`` layout — ``atlan-local-marketplace-app`` keeps ``src/commons``
      and ``src/deployment_orchestrator``, imported as ``commons.…``;
    * pytest rootdir — ``atlan-databricks-app`` has ``tests/incremental``,
      imported as ``incremental.driver``;
    * a test importing a sibling script four levels down, or one under
      ``.claude/skills/…`` — still the app's own code by any reading.

    Those three shapes were 57 of 210 findings on a first pass: a false-positive
    rate that would have taught the fleet to ignore the rule.

    Directories count whether or not they carry ``__init__.py``, since apps use
    regular and namespace packages alike. Dot-directories are included (a repo's
    ``.claude/`` is its own content); only the genuinely uninteresting trees are
    pruned.

    Being generous here only means declining to flag an app's reach into its own
    internals, which is not this rule's business. It can over-exempt if an app
    happens to contain a directory named after a third-party package it also
    imports privately — an acceptable trade at WARN against the alternative.
    Failing to read the tree yields an empty set, which makes every private
    import look foreign: the safe direction, over-report rather than fall silent.
    """

    names: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name in _NOT_IMPORT_ROOTS:
                continue
            if child.is_dir():
                names.add(child.name)
                stack.append(child)
            elif child.suffix == ".py":
                names.add(child.stem)
    return frozenset(names)


#: Underscore-prefixed names that are documented, supported public API despite
#: the leading underscore. The convention is not universal, and flagging these
#: would be simply wrong rather than conservative — ``os._exit`` is the
#: documented way to leave a forked child without running cleanup handlers, and
#: has no non-underscore equivalent.
_PUBLIC_DESPITE_UNDERSCORE = frozenset(
    {
        "os._exit",
        "sys._getframe",
        "sys._current_frames",
        "sys._MEIPASS",  # set by PyInstaller, read by application code
    }
)


def _private_component(dotted: str) -> str | None:
    """The first underscore-prefixed component of *dotted*, if any.

    Dunders are excluded: ``__init__`` and friends are plumbing, not a private
    subpackage, and no app writes them in an import path anyway.
    """
    for part in dotted.split("."):
        if part.startswith("_") and not part.startswith("__"):
            return part
    return None


def _is_own(dotted: str, own_roots: frozenset[str]) -> bool:
    """True when *dotted* names the app's own code."""
    return dotted.split(".")[0] in own_roots


def _message(path: str, component: str) -> str:
    return (
        f"`{path}` reaches into another package's internals — `{component}` is "
        "private. Its owner changes private modules and names without a "
        "deprecation cycle, which is exactly how an SDK refactor stopped fifteen "
        "connector repos from collecting tests (FND-2388). Import the public "
        "equivalent instead, or test through the public behaviour rather than "
        "the internal helper. If no public equivalent exists, that is a gap in "
        "the package worth raising rather than routing around."
    )


def scan_private_imports(
    tree: ast.Module,
    file: str,
    directives: dict[int, _IgnoreDirective],
    own_roots: frozenset[str] = frozenset(),
) -> list[Finding]:
    """Return B008 findings for every foreign-private reach in *tree*."""
    findings: list[Finding] = []
    reported: set[tuple[int, str]] = set()
    # Local name -> dotted module it is bound to, for `import x as y` chains.
    module_aliases: dict[str, str] = {}

    def emit(node: ast.AST, path: str, component: str) -> None:
        key = (node.lineno, path)
        if key in reported:
            return
        reported.add(key)
        findings.append(
            make_finding(
                filename=file,
                rule_id=_RULE_ID,
                node=node,
                message=_message(path, component),
                directives=directives,
            )
        )

    def check(node: ast.AST, dotted: str) -> None:
        if not dotted or _is_own(dotted, own_roots):
            return
        if dotted in _PUBLIC_DESPITE_UNDERSCORE:
            return
        component = _private_component(dotted)
        if component:
            emit(node, dotted, component)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_aliases[alias.asname or alias.name.split(".")[0]] = alias.name
                check(node, alias.name)
        elif isinstance(node, ast.ImportFrom):
            # A relative import resolves against the app's own package, always.
            if node.level:
                continue
            module = node.module or ""
            if _is_own(module, own_roots):
                continue
            component = _private_component(module)
            if component:
                emit(node, module, component)
                continue
            # Public module, private member:
            # `from application_sdk.app.base import _helper`.
            for alias in node.names:
                if _private_component(alias.name):
                    check(node, f"{module}.{alias.name}")

    # Module-qualified *use*: `import application_sdk as sdk` then
    # `sdk.execution._temporal.thing`. The import line above is clean — the
    # private part only appears at the call site.
    #
    # Only the OUTERMOST attribute of a chain is considered. `a.b._c.d` nests
    # one Attribute per dot, so walking them all would report the same reach
    # once per level — `a.b._c.d` and `a.b._c` are different paths and would
    # both survive dedup.
    inner = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node in inner:
            continue
        parts: list[str] = []
        cursor: ast.expr = node
        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
        if not isinstance(cursor, ast.Name):
            continue
        base = module_aliases.get(cursor.id)
        if base is None:
            continue
        check(node, ".".join([base, *reversed(parts)]))

    return findings
