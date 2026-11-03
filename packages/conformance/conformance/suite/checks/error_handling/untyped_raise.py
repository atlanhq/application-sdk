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
    PROTOCOL_DUNDER_BUILTINS,
    VALIDATOR_DECORATOR_BUILTINS,
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


def module_bindings(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Names *tree* binds anywhere, and those bound other than by import or class.

    The first set says which bare names may not mean the builtin of that name
    (``class TypeError(Exception)`` shadows the builtin). The second says which
    names may not mean what their import or module-level class says (``except
    TypeError as InvalidInputError`` rebinds an imported SDK error). Both are
    module-wide, like the ``_sdk_errors`` set they guard.
    """
    bound: set[str] = set()
    rebound: set[str] = set()
    top_level_classes = {id(n) for n in tree.body if isinstance(n, ast.ClassDef)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            bound.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ClassDef) and id(node) in top_level_classes:
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            rebound.add(node.id)
        elif isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ) or (isinstance(node, ast.ExceptHandler) and node.name):
            rebound.add(node.name)  # type: ignore[arg-type]
        elif isinstance(node, ast.arg):
            rebound.add(node.arg)
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            rebound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            rebound.add(node.rest)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            rebound.update(node.names)
    return frozenset(bound | rebound), frozenset(rebound)


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class UntypedRaiseMixin:
    """Rule methods for E012, E013, and E018 (untyped-raise category)."""

    # ── E012 context helpers ──────────────────────────────────────────────────

    def _builtin_type(self, node: ast.expr) -> type[BaseException] | None:
        """The builtin exception class *node* names, if it provably names one.

        A bare name counts only when the module never binds it, and
        ``builtins.X`` only when ``builtins`` is the stdlib module.
        """
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Name):
            if node.id in self._bound_names:
                return None
            name = node.id
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if self._import_origins.get(node.value.id) != "builtins":
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

    def _handler_catches_builtin(
        self, handler: ast.ExceptHandler, raised_type: type[BaseException]
    ) -> bool:
        if handler.type is None:
            return True
        caught_nodes = (
            handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        )
        return any(
            (caught_type := self._builtin_type(caught_node)) is not None
            and issubclass(raised_type, caught_type)
            for caught_node in caught_nodes
        )

    def _in_interop_context(self, node: ast.Raise) -> bool:
        if not self._function_stack:
            return False
        func = self._function_stack[-1]
        if isinstance(func, ast.FunctionDef) and func.name in INTEROP_METHODS:
            return True
        decorators = _get_decorator_names(func)
        if decorators & INTEROP_DECORATORS:
            return True
        # The remaining carve-outs exempt one named builtin, so the raise must
        # provably be that builtin (not a same-named module class).
        raised = self._builtin_type(node.exc) if node.exc is not None else None
        if raised is None:
            return False
        if raised.__name__ in PROTOCOL_DUNDER_BUILTINS.get(
            func.name, frozenset()
        ) and self._is_class_method(func):
            return True
        return any(
            raised.__name__ in VALIDATOR_DECORATOR_BUILTINS.get(name, frozenset())
            for name in decorators
        )

    def _is_class_method(self, func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """True when *func* is defined directly in the innermost class body."""
        return bool(self._class_stack) and any(
            stmt is func for stmt in self._class_stack[-1].body
        )

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
        if translated.exc is None:
            return False
        raised_type = (
            translated.exc.func
            if isinstance(translated.exc, ast.Call)
            else translated.exc
        )
        # The proven set is module-wide; a name rebound anywhere (e.g. this
        # handler's ``as`` target) may not be the SDK class at this raise.
        root = _root_name(raised_type)
        if root is None or root in self._rebound_names:
            return False
        return names_sdk_error(self._sdk_errors, _raised_type_name(translated))

    def _has_typed_translation_boundary(self, node: ast.Raise) -> bool:
        if node.exc is None:
            return False
        raised_type = self._builtin_type(node.exc)
        if raised_type is None:
            return False
        for try_node in reversed(self._try_stack):
            for handler in try_node.handlers:
                if self._handler_catches_builtin(handler, raised_type):
                    # An except* handler re-raises inside an ExceptionGroup,
                    # never as the bare typed error, so it is no boundary.
                    return isinstance(
                        try_node, ast.Try
                    ) and self._handler_translates_to_typed_error(handler)
        return False

    def _check_p012(self, node: ast.Raise) -> None:
        if node.exc is None:
            return
        exc_name = _raise_exc_name(node.exc)
        if exc_name not in BUILTIN_RAISES or not self._e012_in_scope:
            return
        if self._in_interop_context(node) or self._has_typed_translation_boundary(node):
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
