"""E022 — a class body binds ``DEFAULT_ERROR_CODE`` in the SDK's legacy ``AAF-*`` vocabulary.

The SDK's one error vocabulary is the ``code`` ClassVar. ``DEFAULT_ERROR_CODE``
was the second, carrying an ``AAF-*`` ``ErrorCode`` beside ``code`` (FND-4299).
The SDK no longer declares it on any class, and its ``AAF-*`` names warn on
access until v4.0.0 removes them.

The rule is an allowlist of two syntactic shapes, both local to the class:

* a base of the class is an SDK class that carries an ``AAF-*`` code
  (:data:`SDK_LEGACY_CODED_CLASSES`), imported by name from ``application_sdk``;
* the bound value or its annotation names the SDK's ``ErrorCode`` or one of its
  ``AAF-*`` constants, imported from ``application_sdk.errors`` or reached as
  ``application_sdk.errors.<NAME>``.

An app's own ``DEFAULT_ERROR_CODE`` on its own mixin, typed with its own code
class, matches neither shape and is not flagged: that is the app's design, not
the SDK's legacy vocabulary.
"""

from __future__ import annotations

import ast

from ._constants import SDK_LEGACY_CODE_NAMES, SDK_LEGACY_CODED_CLASSES

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


class LegacyErrorCodeMixin:
    """Rule method for E022 (legacy-error-code)."""

    _import_origins: dict[str, str]

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

    def _sdk_legacy_base(self, node: ast.ClassDef) -> str | None:
        for base in node.bases:
            origin = self._sdk_origin(base)
            if origin is None or not origin.startswith("application_sdk."):
                continue
            module, _, name = origin.rpartition(".")
            if module.startswith("application_sdk.errors"):
                continue
            if name in SDK_LEGACY_CODED_CLASSES:
                return name
        return None

    # ── E022 ─────────────────────────────────────────────────────────────────

    def _check_e022(self, node: ast.ClassDef) -> None:
        bindings = _default_error_code_bindings(node)
        if not bindings:
            return
        base = self._sdk_legacy_base(node)
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
