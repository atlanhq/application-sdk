"""B010 ``SdkGlobalRebind`` — flag an app monkeypatching the SDK at runtime.

Runs against *consumer apps* (scope ``app``). Flags any statement that rebinds
or deletes an attribute of something the file imported from
``application_sdk`` — a module, a class, any object:

* ``svc._CREDENTIAL_KEYS = svc._CREDENTIAL_KEYS | {"kerberos"}`` after
  ``import application_sdk.handler.service as svc``;
* ``service._normalize_credentials = wrapper`` after
  ``from application_sdk.handler import service``;
* ``Handler.some_hook = patched`` after
  ``from application_sdk.handler.base import Handler``;
* ``application_sdk.constants.X = 1`` after ``import application_sdk.constants``;
* ``x.attr += 1``, ``x.attr: T = v``, ``del x.attr``, and
  ``setattr(x, "attr", v)`` / ``delattr(x, "attr")`` on any such ``x``.

Why this rule exists
--------------------

FND-4688. Two connector apps changed the SDK's request normalization by
rebinding globals of ``application_sdk.handler.service`` at import time: one
extended ``_CREDENTIAL_KEYS`` with ``kerberos``, the other replaced
``_normalize_credentials`` with a wrapper. The SDK kept extra indirection
layers only so those patches kept working, and any refactor of the handler
would have broken both apps silently — a rebound name nothing reads anymore
raises nothing. The patches also never reached the consolidated API host,
which does not import that module, so the behaviour already differed between
surfaces.

B008 reports the private *name* only in some of these shapes (not
``from application_sdk.handler import service``, which binds a module through
a public path), and not at all when the rebound name is public. The hazard is
the rebind, whatever the name looks like: the SDK reads its own globals and
class attributes on the assumption that it set them.

What is not flagged
-------------------

* An attribute of an object the app built (``client = SQLClient(); client.x =
  1``): it is the app's own state, and its root name is not an import.
* ``monkeypatch.setattr(...)`` and ``unittest.mock.patch(...)`` in tests: both
  undo the change when the test ends, and neither is the ``setattr`` builtin.
* A relative import, or an import rooted at the app's own packages.

Tier
----

WARN, not BLOCK: the two known patches have a public replacement
(``Handler.extra_credential_keys``, ``Handler.prepare_request_body``) and the
fix is app-side.
"""

from __future__ import annotations

import ast

from conformance.suite.checks._ast_common import _IgnoreDirective, make_finding
from conformance.suite.schema.findings import Finding

_RULE_ID = "B010"
_SDK_ROOT = "application_sdk"

#: Rebinds with a known public replacement, keyed by the dotted path rebound.
_REPLACEMENTS: dict[str, str] = {
    "application_sdk.handler.service._CREDENTIAL_KEYS": (
        "declare the extra keys as `extra_credential_keys` on the app's "
        "Handler subclass"
    ),
    "application_sdk.handler.service._normalize_credentials": (
        "override `prepare_request_body` on the app's Handler subclass to "
        "rewrite the request body before credential normalization"
    ),
}


def _is_sdk(dotted: str) -> bool:
    return dotted == _SDK_ROOT or dotted.startswith(_SDK_ROOT + ".")


def _sdk_bindings(tree: ast.Module) -> dict[str, str]:
    """Local name -> the dotted SDK path it was imported as, for this file."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_sdk(alias.name):
                    continue
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    # `import application_sdk.a.b` binds `application_sdk`.
                    bound[alias.name.split(".")[0]] = _SDK_ROOT
        elif isinstance(node, ast.ImportFrom):
            if node.level or not _is_sdk(node.module or ""):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _dotted(node: ast.expr, bound: dict[str, str]) -> str | None:
    """The SDK path *node* names, when it is an attribute chain on an SDK import."""
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if not isinstance(cursor, ast.Name):
        return None
    base = bound.get(cursor.id)
    if base is None:
        return None
    return ".".join([base, *reversed(parts)])


def _targets(node: ast.AST) -> list[ast.expr]:
    """The expressions *node* binds or deletes, tuple and list targets unpacked."""
    raw: list[ast.expr]
    if isinstance(node, ast.Assign):
        raw = list(node.targets)
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        raw = [node.target]
    elif isinstance(node, ast.Delete):
        raw = list(node.targets)
    else:
        return []
    out: list[ast.expr] = []
    while raw:
        target = raw.pop()
        if isinstance(target, ast.Tuple | ast.List):
            raw.extend(target.elts)
        elif isinstance(target, ast.Starred):
            raw.append(target.value)
        else:
            out.append(target)
    return out


def _setattr_path(node: ast.Call, bound: dict[str, str]) -> str | None:
    """``setattr(x, "name", v)`` / ``delattr(x, "name")`` on an SDK import."""
    if not isinstance(node.func, ast.Name) or node.func.id not in {
        "setattr",
        "delattr",
    }:
        return None
    if len(node.args) < 2:
        return None
    owner = _dotted(node.args[0], bound)
    if owner is None:
        return None
    name = node.args[1]
    if isinstance(name, ast.Constant) and isinstance(name.value, str):
        return f"{owner}.{name.value}"
    return f"{owner}.<dynamic>"


def _message(path: str) -> str:
    replacement = _REPLACEMENTS.get(path)
    fix = (
        f"Instead, {replacement}."
        if replacement
        else (
            "Use the SDK's public extension point for this behaviour instead; if "
            "there is none, that is a gap worth raising with the SDK rather than "
            "patching around."
        )
    )
    return (
        f"`{path}` is rebound at runtime. The SDK reads its own module globals "
        "and class attributes on the assumption that it set them: a refactor "
        "that stops reading this one breaks the app silently, and the patch "
        "reaches only processes that import the patched module (FND-4688). " + fix
    )


def scan_sdk_global_rebind(
    tree: ast.Module,
    file: str,
    directives: dict[int, _IgnoreDirective],
) -> list[Finding]:
    """Return B010 findings for every rebind of an SDK-imported attribute in *tree*."""
    bound = _sdk_bindings(tree)
    if not bound:
        return []
    findings: list[Finding] = []
    reported: set[tuple[int, str]] = set()

    def emit(node: ast.AST, path: str) -> None:
        key = (getattr(node, "lineno", 0), path)
        if key in reported:
            return
        reported.add(key)
        findings.append(
            make_finding(
                filename=file,
                rule_id=_RULE_ID,
                node=node,
                message=_message(path),
                directives=directives,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            path = _setattr_path(node, bound)
            if path is not None:
                emit(node, path)
            continue
        for target in _targets(node):
            if not isinstance(target, ast.Attribute):
                continue
            path = _dotted(target, bound)
            if path is not None:
                emit(node, path)
    return findings
