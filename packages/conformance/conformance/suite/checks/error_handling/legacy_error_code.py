"""E022 — a class body binds ``DEFAULT_ERROR_CODE`` in the SDK's legacy ``AAF-*`` vocabulary.

The SDK's one error vocabulary is the ``code`` ClassVar. ``DEFAULT_ERROR_CODE``
was the second, carrying an ``AAF-*`` ``ErrorCode`` beside ``code`` (FND-4299).
The SDK no longer declares it on any class, and its ``AAF-*`` names warn on
access until v4.0.0 removes them.

The rule is an allowlist of two syntactic shapes, both local to the class:

* the class reaches an SDK class that carries an ``AAF-*`` code
  (:data:`SDK_LEGACY_CODED_CLASSES`) through its bases: directly, imported by
  name from ``application_sdk`` or through an ``application_sdk`` module alias,
  or through app-defined intermediates in the same module or imported from the
  scanned repo (the in-repo resolution :mod:`._sdk_errors` and E021 use);
* the bound value or its annotation names the SDK's ``ErrorCode`` or one of its
  ``AAF-*`` constants, imported from ``application_sdk.errors`` or reached as
  ``application_sdk.errors.<NAME>``.

An app's own ``DEFAULT_ERROR_CODE`` on its own mixin, typed with its own code
class, matches neither shape and is not flagged: that is the app's design, not
the SDK's legacy vocabulary.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ._constants import SDK_LEGACY_CODE_NAMES, SDK_LEGACY_CODED_CLASSES
from ._sdk_errors import _Resolver as _SdkErrorResolver

_SDK_PACKAGE = "application_sdk"

_ERRORS_MODULES: frozenset[str] = frozenset(
    {"application_sdk.errors", "application_sdk.errors._legacy_codes"}
)


def _default_error_code_bindings(
    node: ast.ClassDef,
) -> list[ast.Assign | ast.AnnAssign]:
    """Statements in *node*'s own body that bind ``DEFAULT_ERROR_CODE``."""
    found: list[ast.Assign | ast.AnnAssign] = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign):
            targets: list[ast.expr] = [stmt.target]
        elif isinstance(stmt, ast.Assign):
            targets = stmt.targets
        else:
            continue
        if any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_ERROR_CODE" for t in targets
        ):
            found.append(stmt)
    return found


def _dotted(expr: ast.expr) -> str | None:
    """``a.b.c`` for a Name/Attribute chain, else ``None``."""
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name):
        return None
    parts.append(expr.id)
    return ".".join(reversed(parts))


def _sdk_legacy_name(module: str, name: str) -> str | None:
    """*name* when it is an SDK legacy-coded class imported from *module*.

    A name from ``application_sdk.errors`` never matches: the canonical
    ``AppError`` there carries no ``AAF-*`` code, only ``application_sdk.app``'s
    deprecated shim of the same name does.
    """
    if module == "application_sdk.errors" or module.startswith(
        "application_sdk.errors."
    ):
        return None
    return name if name in SDK_LEGACY_CODED_CLASSES else None


def _sdk_module_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names bound to an ``application_sdk`` module, mapped to that module."""
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] != _SDK_PACKAGE:
                    continue
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    aliases[_SDK_PACKAGE] = _SDK_PACKAGE
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and node.module.split(".")[0] == _SDK_PACKAGE
        ):
            for alias in node.names:
                if not alias.name[:1].isupper():
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


class _Resolver(_SdkErrorResolver):
    """Reuses :mod:`._sdk_errors`' in-repo module loading to answer: which SDK
    legacy-coded class, if any, does a class reach through its bases?"""

    def name_base(
        self,
        tree: ast.Module,
        path: Path | None,
        name: str,
        visited: frozenset[tuple[Path | None, str]],
    ) -> str | None:
        key = (path, name)
        if key in visited:
            return None
        visited = visited | {key}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if (alias.asname or alias.name) != name:
                    continue
                if (
                    node.level == 0
                    and node.module is not None
                    and (node.module.split(".")[0] == _SDK_PACKAGE)
                ):
                    return _sdk_legacy_name(node.module, alias.name)
                target = self.module_file(node.module, node.level, path)
                loaded = self.load(target) if target is not None else None
                if loaded is None:
                    return None
                return self.name_base(loaded, target, alias.name, visited)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return self.class_base(tree, path, node, visited)
        return None

    def class_base(
        self,
        tree: ast.Module,
        path: Path | None,
        node: ast.ClassDef,
        visited: frozenset[tuple[Path | None, str]] = frozenset(),
    ) -> str | None:
        for base in node.bases:
            if isinstance(base, ast.Name):
                found = self.name_base(tree, path, base.id, visited)
            else:
                dotted = _dotted(base)
                found = None
                if dotted is not None and "." in dotted:
                    head, _, rest = dotted.partition(".")
                    module = _sdk_module_aliases(tree).get(head)
                    if module is not None:
                        qualified = f"{module}.{rest}"
                        owner, _, attr = qualified.rpartition(".")
                        found = _sdk_legacy_name(owner, attr)
            if found is not None:
                return found
        return None


def legacy_coded_bases(
    tree: ast.Module, *, path: Path | None = None, root: Path | None = None
) -> dict[str, str]:
    """Each class in *tree* that reaches an SDK legacy-coded class, mapped to it.

    Without *root*, bases imported from the scanned repo cannot be followed;
    same-module intermediates and SDK imports still resolve.
    """
    resolver = _Resolver(root)
    module_path = path.resolve() if path is not None else None
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            base = resolver.class_base(tree, module_path, node)
            if base is not None:
                found.setdefault(node.name, base)
    return found


class LegacyErrorCodeMixin:
    """Rule method for E022 (legacy-error-code)."""

    _import_origins: dict[str, str]
    _legacy_coded_bases: dict[str, str]

    def _add(self, rule_id: str, node: ast.AST, message: str) -> None: ...

    def _sdk_origin(self, expr: ast.expr) -> str | None:
        """The ``module.attr`` an expression names through this module's imports."""
        dotted = _dotted(expr)
        if dotted is None:
            return None
        head, _, rest = dotted.partition(".")
        origin = self._import_origins.get(head)
        if origin is None:
            return None
        return f"{origin}.{rest}" if rest else origin

    def _names_legacy_vocabulary(self, expr: ast.expr | None) -> bool:
        if expr is None:
            return False
        for sub in ast.walk(expr):
            if not isinstance(sub, ast.Name | ast.Attribute):
                continue
            origin = self._sdk_origin(sub)
            if origin is None:
                continue
            module, _, name = origin.rpartition(".")
            if module in _ERRORS_MODULES and name in SDK_LEGACY_CODE_NAMES:
                return True
        return False

    # ── E022 ─────────────────────────────────────────────────────────────────

    def _check_e022(self, node: ast.ClassDef) -> None:
        bindings = _default_error_code_bindings(node)
        if not bindings:
            return
        base = self._legacy_coded_bases.get(node.name)
        for stmt in bindings:
            annotation = stmt.annotation if isinstance(stmt, ast.AnnAssign) else None
            legacy_value = self._names_legacy_vocabulary(
                stmt.value
            ) or self._names_legacy_vocabulary(annotation)
            if base is None and not legacy_value:
                continue
            reason = (
                f"overrides the AAF-* code {node.name} inherits from the SDK's {base}"
                if base is not None
                else "binds it to the SDK's deprecated AAF-* ErrorCode vocabulary"
            )
            self._add(
                "E022",
                stmt,
                f"{node.name} declares DEFAULT_ERROR_CODE: it {reason}. The SDK's "
                f"one error vocabulary is the code ClassVar; AAF-* codes are "
                f"deprecated and removed in v4.0.0. Delete the DEFAULT_ERROR_CODE "
                f"line and give the class its own code ClassVar.",
            )
