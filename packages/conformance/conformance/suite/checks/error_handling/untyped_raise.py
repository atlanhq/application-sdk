"""E012, E013, E018 — untyped / legacy raise rules."""

from __future__ import annotations

import ast
import builtins

from ._constants import (
    ACTIVITY_DECORATORS,
    BUILTIN_RAISES,
    INTEROP_DECORATORS,
    INTEROP_METHODS,
    LEAF_CLASSES,
    LEGACY_ATLAN_ERRORS,
)
from ._helpers import _get_decorator_names, _raise_exc_name
from ._sdk_errors import names_sdk_error


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _raised_type_name(raise_node: ast.Raise) -> str | None:
    if raise_node.exc is None:
        return None
    raised_type = (
        raise_node.exc.func if isinstance(raise_node.exc, ast.Call) else raise_node.exc
    )
    return _qualified_name(raised_type)


def _builtin_type(node: ast.expr) -> type[BaseException] | None:
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id != "builtins":
            return None
        name = node.attr
    else:
        return None
    exception_type = getattr(builtins, name, None)
    if not isinstance(exception_type, type) or not issubclass(
        exception_type, BaseException
    ):
        return None
    return exception_type


def _handler_catches_builtin(handler: ast.ExceptHandler, raised_name: str) -> bool:
    raised_type = getattr(builtins, raised_name, None)
    if not isinstance(raised_type, type) or not issubclass(raised_type, BaseException):
        return False
    if handler.type is None:
        return True
    caught_nodes = (
        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    )
    return any(
        (caught_type := _builtin_type(caught_node)) is not None
        and issubclass(raised_type, caught_type)
        for caught_node in caught_nodes
    )


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

    def _handler_translates_to_typed_error(self, handler: ast.ExceptHandler) -> bool:
        statements = [
            stmt
            for stmt in handler.body
            if not (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            )
        ]
        if len(statements) != 1 or not isinstance(statements[0], ast.Raise):
            return False
        translated = statements[0]
        return translated.exc is not None and names_sdk_error(
            self._sdk_errors, _raised_type_name(translated)
        )

    def _has_typed_translation_boundary(self, node: ast.Raise, exc_name: str) -> bool:
        for try_node in reversed(self._try_stack):
            for handler in try_node.handlers:
                if _handler_catches_builtin(handler, exc_name):
                    return self._handler_translates_to_typed_error(handler)
        return False

    def _check_p012(self, node: ast.Raise) -> None:
        if node.exc is None:
            return
        exc_name = _raise_exc_name(node.exc)
        if exc_name not in BUILTIN_RAISES:
            return
        if self._in_interop_context() or self._has_typed_translation_boundary(
            node, exc_name
        ):
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

        raised_type = exc_node.func if isinstance(exc_node, ast.Call) else exc_node
        attributes: list[str] = []
        while isinstance(raised_type, ast.Attribute):
            attributes.append(raised_type.attr)
            raised_type = raised_type.value

        imported_type: str | None = None
        if isinstance(raised_type, ast.Name):
            origin = self._import_origins.get(raised_type.id)
            if origin is not None:
                imported_type = ".".join((origin, *reversed(attributes)))

        # Backend libraries can expose control-flow exceptions from an
        # `exceptions` module. A same-named type from such a non-SDK module is not
        # an application_sdk.errors AppError leaf. Keep arbitrary external
        # origins and unresolved names conservative.
        if exc_name != "CancelledError" and imported_type is not None:
            origin_module = imported_type.rpartition(".")[0]
            if "exceptions" in origin_module.split(
                "."
            ) and not imported_type.startswith("application_sdk.errors."):
                return
        # asyncio.CancelledError is stdlib cancellation control flow. Keep its
        # precise existing exception: only exact asyncio origins are exempt, so
        # a same-named class from another module is still checked.
        elif imported_type in {
            "asyncio.CancelledError",
            "asyncio.exceptions.CancelledError",
        }:
            return

        # Sanctioned bare-parent form: InternalError(classification_pending=True)
        if exc_name == "InternalError":
            for kw in kws:
                if (
                    kw.arg == "classification_pending"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    return

        self._add(
            "E018",
            node,
            f"raise {exc_name} uses a bare parent leaf without a domain-specific subclass "
            f"that overrides 'code' — collapses all failures of this category into one "
            f"dashboard bucket. Define a subclass with a specific code constant. "
            f"Only acceptable bare-parent form: InternalError(classification_pending=True).",
        )
