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
are matched; a same-named local helper is not, because the name must resolve to
an ``application_sdk`` import in the same file.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

#: The prefix-level transfer helpers. Single-object transfers are out of scope.
_PREFIX_TRANSFERS: frozenset[str] = frozenset({"upload_prefix", "download_prefix"})

_SDK_STORAGE_ROOT = "application_sdk"


def _sdk_storage_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return ``(direct_names, module_aliases)`` bound to SDK storage in this file.

    ``direct_names`` are prefix helpers pulled in by
    ``from application_sdk.storage import upload_prefix`` (honouring ``as``
    aliases). ``module_aliases`` are names bound to an SDK storage *module* by
    ``import application_sdk.storage as storage`` or
    ``from application_sdk import storage``, so an attribute call through them
    can be resolved.

    Anything not traceable to an ``application_sdk`` import is left alone: a
    local ``upload_prefix`` helper is a different function that happens to share
    a name.
    """
    direct: set[str] = set()
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not (module == _SDK_STORAGE_ROOT or module.startswith(f"{_SDK_STORAGE_ROOT}.")):
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name in _PREFIX_TRANSFERS:
                    direct.add(bound)
                elif alias.name == "storage" or alias.name.endswith("ops"):
                    # `from application_sdk import storage`
                    aliases.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith(f"{_SDK_STORAGE_ROOT}."):
                    continue
                if alias.asname:
                    aliases.add(alias.asname)
                else:
                    # `import application_sdk.storage` binds the root package;
                    # the call site then reads application_sdk.storage.x(...).
                    aliases.add(alias.name.split(".")[0])
    return direct, aliases


def _called_prefix_helper(
    node: ast.Call, direct: set[str], aliases: set[str]
) -> str | None:
    """Name of the prefix helper *node* calls, or ``None``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id if func.id in direct else None
    if isinstance(func, ast.Attribute) and func.attr in _PREFIX_TRANSFERS:
        # Resolve the receiver chain's root: storage.upload_prefix(...) or
        # application_sdk.storage.upload_prefix(...).
        receiver: ast.expr = func.value
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        if isinstance(receiver, ast.Name) and receiver.id in aliases:
            return func.attr
    return None


def check_p044(
    tree: ast.AST,
    filename: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Emit P044 for each prefix-level transfer the app performs itself."""
    direct, aliases = _sdk_storage_bindings(tree)
    if not direct and not aliases:
        return []

    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        helper = _called_prefix_helper(node, direct, aliases)
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
