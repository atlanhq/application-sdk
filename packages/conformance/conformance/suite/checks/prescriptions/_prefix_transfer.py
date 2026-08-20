"""P044 DirectStoragePrefixTransfer — app moves prefixes itself.

``storage.upload_prefix`` / ``download_prefix`` are real SDK functions, so this
is *not* the build-your-own-store shape :mod:`._store_construction` (P009)
describes. It is the sanctioned seam used one level too low: the app takes on
the transfer instead of declaring the data on its contract and letting the SDK
move it.

Why no existing rule covers it
------------------------------
* **P009** fires on constructing a store or cloud client — an app calling an SDK
  function is not that.
* **P030** / **P042** are gated on ``self_deployed_runtime: true``, so for a
  direct-flow app the entire SDR series short-circuits before it can ask whether
  ``App.upload()`` is used at all.

Deliberately a *presence* check, not an absence check
----------------------------------------------------
"App never calls ``App.upload()``" is the wrong predicate outside SDR: for
task-to-task data ``persist_file_refs`` uploads every ephemeral
``FileReference`` in a task's output and ``materialize_file_refs`` fetches it
back, so an app that declares references correctly needs no explicit upload at
all. Flagging absence would fire on precisely the apps doing it right. Flagging
the prefix call is unambiguous in both modes.

Scope of the match
------------------
Only the two *prefix* transfer helpers, matched on the callee name plus the
import that brought it in. Single-object helpers (``upload_file``,
``download_file``) are out of scope: they are the right tool for the cases where
a caller genuinely holds one file and no contract boundary to hang a reference
on. Both the ``from application_sdk.storage import upload_prefix`` form and the
attribute form (``storage.upload_prefix(...)``, ``ops.download_prefix(...)``)
are matched.

The gate is the *import path*, not the ``application_sdk`` prefix: the name has
to come from one of the two modules that actually expose the prefix helpers —
``application_sdk.storage``, or ``application_sdk.storage.batch`` where they are
defined. Anything else is a different function that happens to share a name and
is left alone: a same-named local helper, a same-named symbol imported from
another ``application_sdk`` subpackage, or an attribute call through an alias
bound to an unrelated ``application_sdk`` module.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: The prefix-level transfer helpers. Single-object transfers are out of scope.
_PREFIX_TRANSFERS: frozenset[str] = frozenset({"upload_prefix", "download_prefix"})

#: The only import paths that expose the prefix helpers. ``batch`` defines them;
#: ``storage`` (the package) re-exports them. Matching the full dotted path
#: rather than an ``application_sdk`` prefix is what keeps unrelated
#: ``application_sdk.*`` sources — which cannot supply these names — out.
_PREFIX_MODULE_PATHS: frozenset[str] = frozenset(
    {"application_sdk.storage", "application_sdk.storage.batch"}
)


def _dotted_name(node: ast.expr) -> str | None:
    """Flatten ``a.b.c`` to ``"a.b.c"``; ``None`` if the chain is not all names."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _sdk_storage_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return ``(direct_names, receivers)`` bound to SDK storage in this file.

    ``direct_names`` are prefix helpers pulled in by
    ``from application_sdk.storage import upload_prefix`` (honouring ``as``
    aliases). ``receivers`` are dotted receiver strings that resolve to a module
    exposing the prefix helpers, so an attribute call through one can be
    resolved: ``storage`` for ``from application_sdk import storage``, ``ops``
    for ``import application_sdk.storage.batch as ops``, and
    ``application_sdk.storage`` for a plain ``import application_sdk.storage``.

    Both gates key on the import *path*, not the local alias text: any import
    naming ``application_sdk.storage`` or ``application_sdk.storage.batch``
    registers its bound name, whatever that name is. That is what matches
    ``from application_sdk.storage import batch as ops`` while leaving
    ``from application_sdk.contracts import storage`` alone — the alias text is
    the same shape in both, the import source is not.
    """
    direct: set[str] = set()
    receivers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name in _PREFIX_TRANSFERS and module in _PREFIX_MODULE_PATHS:
                    direct.add(bound)
                elif f"{module}.{alias.name}" in _PREFIX_MODULE_PATHS:
                    # `from application_sdk import storage`,
                    # `from application_sdk.storage import batch[ as ops]`.
                    receivers.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _PREFIX_MODULE_PATHS:
                    continue
                if alias.asname:
                    receivers.add(alias.asname)
                else:
                    # `import application_sdk.storage[.batch]` binds the root
                    # package; the call site reads the full dotted path back,
                    # and every ancestor package is bound along with it.
                    segments = alias.name.split(".")
                    for end in range(1, len(segments) + 1):
                        path = ".".join(segments[:end])
                        if path in _PREFIX_MODULE_PATHS:
                            receivers.add(path)
    return direct, receivers


def _called_prefix_helper(
    node: ast.Call, direct: set[str], receivers: set[str]
) -> str | None:
    """Name of the prefix helper *node* calls, or ``None``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id if func.id in direct else None
    if isinstance(func, ast.Attribute) and func.attr in _PREFIX_TRANSFERS:
        # storage.upload_prefix(...) or application_sdk.storage.upload_prefix(...)
        receiver = _dotted_name(func.value)
        if receiver is not None and receiver in receivers:
            return func.attr
    return None


def check_p044(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P044 for each prefix-level transfer the app performs itself."""
    direct, receivers = _sdk_storage_bindings(tree)
    if not direct and not receivers:
        return []

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        helper = _called_prefix_helper(node, direct, receivers)
        if helper is None:
            continue
        direction = "upload" if helper.startswith("upload") else "download"
        findings.append(
            make_finding(
                filename=filename,
                rule_id="P044",
                node=node,
                message=(
                    f"{helper}() moves a whole prefix from app code. This is an SDK "
                    "function used one level below the storage contract, so P009 "
                    "(build-your-own-store) does not fire and — outside SDR — neither "
                    "does P030. Declare the data on the contract as a FileReference "
                    "for task-to-task flow, or call "
                    f"App.{direction}() for a phase hand-off. Hoist that call to "
                    "run()/@entrypoint rather than leaving it where this one sits: "
                    "App.upload()/download() are framework tasks, so calling them "
                    "inside a @task trades this finding for P008. What the prefix "
                    "call gives up is dual-write routing, the canonical artifact "
                    "prefix, @task retry/replay, and the per-file SHA-256 sidecar "
                    "that detects a partial transfer a directory-level check cannot. "
                    "For a genuine bulk sync with no contract boundary, suppress with "
                    "'# conformance: ignore[P044] <reason>'."
                ),
                directives=directives,
            )
        )
    return findings
