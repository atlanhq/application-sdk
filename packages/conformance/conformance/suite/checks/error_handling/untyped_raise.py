"""E012, E013, E018 — untyped / legacy raise rules."""

from __future__ import annotations

import ast

from ._constants import (
    ACTIVITY_DECORATORS,
    BUILTIN_RAISES,
    INTEROP_DECORATORS,
    INTEROP_METHODS,
    LEAF_CLASSES,
    LEGACY_ATLAN_ERRORS,
)
from ._helpers import _get_decorator_names, _raise_exc_name


class UntypedRaiseMixin:
    """Rule methods for E012, E013, and E018 (untyped-raise category)."""

    # ── E012 context helpers ──────────────────────────────────────────────────

    def _in_interop_context(self) -> bool:
        if not self._function_stack:
            return False
        func = self._function_stack[-1]
        if isinstance(func, ast.FunctionDef) and func.name in INTEROP_METHODS:
            return True
        return bool(_get_decorator_names(func) & INTEROP_DECORATORS)

    def _in_activity_context(self) -> bool:
        for func in self._function_stack:
            if _get_decorator_names(func) & ACTIVITY_DECORATORS:
                return True
        return False

    # ── E012 ─────────────────────────────────────────────────────────────────

    def _check_p012(self, node: ast.Raise) -> None:
        if node.exc is None:
            return
        exc_name = _raise_exc_name(node.exc)
        if exc_name not in BUILTIN_RAISES:
            return
        if self._in_interop_context():
            return
        activity_note = (
            " (NOTE: inside activity/task — AE receives an opaque string, no typed envelope)"
            if self._in_activity_context()
            else ""
        )
        self._add(
            "E012",
            node,
            f"raise {exc_name} where a typed AppError leaf should be used{activity_note}. "
            f"Replace with a domain-specific subclass from application_sdk.errors.",
        )

    # ── E013 ─────────────────────────────────────────────────────────────────

    def _check_p013(self, node: ast.Raise) -> None:
        if node.exc is None:
            return
        exc_name = _raise_exc_name(node.exc)
        # Also flag names aliased from the legacy module (e.g. `import IOError as IOE`)
        if exc_name not in LEGACY_ATLAN_ERRORS and exc_name not in self._legacy_aliases:
            return
        # IOError is also the Python builtin alias for OSError — only flag the literal
        # name when it was imported from application_sdk.common.error_codes; aliased
        # imports (tracked in _legacy_aliases) are always from the legacy module.
        if exc_name == "IOError" and not self._atlan_ioerror_imported:
            return
        self._add(
            "E013",
            node,
            f"raise {exc_name} uses the deprecated AtlanError stack "
            f"(emits DeprecationWarning; produces no typed wire envelope; "
            f"scheduled for removal in v4.0). "
            f"Replace with the appropriate leaf from application_sdk.errors.",
        )

    # ── E018 ─────────────────────────────────────────────────────────────────

    def _check_p018(self, node: ast.Raise) -> None:
        if node.exc is None:
            return
        exc_node = node.exc
        exc_name = _raise_exc_name(exc_node)
        kws: list[ast.keyword] = []
        if isinstance(exc_node, ast.Call):
            kws = exc_node.keywords

        if exc_name not in LEAF_CLASSES:
            return

        # Sanctioned bare-parent form: InternalError(classification_pending=True)
        if exc_name == "InternalError":
            for kw in kws:
                if kw.arg == "classification_pending":
                    if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        return

        self._add(
            "E018",
            node,
            f"raise {exc_name} uses a bare parent leaf without a domain-specific subclass "
            f"that overrides 'code' — collapses all failures of this category into one "
            f"dashboard bucket. Define a subclass with a specific code constant. "
            f"Only acceptable bare-parent form: InternalError(classification_pending=True).",
        )
