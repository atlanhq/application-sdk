"""E008 — an optional-import guard that swallows the ImportError."""

from __future__ import annotations

import ast

from .._constants import _OPTIONAL_IMPORT_TYPES
from .._helpers import (
    _any_logging_in,
    _body_always_raises,
    _body_has_bypassing_exit,
    _get_name,
    _iter_shallow,
)


class E008Mixin:
    """Rule methods for E008."""

    # ── E008 ─────────────────────────────────────────────────────────────────

    def _check_p008(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            return
        exc_type = _get_name(node.type)
        if exc_type not in _OPTIONAL_IMPORT_TYPES:
            return
        # An unconditional cause-preserving re-raise surfaces the missing
        # dependency instead of hiding it behind a silent fallback. An explicit
        # ``from`` replaces the implicit context, so it only counts when the
        # cause carries the caught ImportError.
        if (
            _body_always_raises(node.body)
            and not _body_has_bypassing_exit(node.body)
            and _raises_chain_caught(node)
        ):
            return
        if _any_logging_in(node.body):
            return
        # The rule's own text accepts an explanatory comment on the guard.
        end = getattr(node, "end_lineno", None) or node.lineno
        if any(node.lineno <= line <= end for line in self._comment_lines):
            return
        if any(self._is_reporter_call(stmt) for stmt in node.body):
            return
        self._add(
            "E008",
            node,
            f"'except {exc_type}' with no logging — import failures are silently hidden. "
            f"Log at DEBUG if the module is preferred but not required, or add a comment "
            f"explaining the optional dependency and use the suppression directive.",
        )

    #: Leaf-name fragments of a call that reports a problem to its user.
    _REPORTER_WORDS = ("error", "warn", "report", "fail", "fatal", "abort", "die")

    def _is_reporter_call(self, stmt: ast.stmt) -> bool:
        """A call that reports the missing module with a message, instead of a log.

        ``_error(path, "optional_codegen missing; cannot verify")``: the
        callee's leaf name says it reports a problem (error/warn/report/fail/
        fatal/abort/die) and one argument is a non-empty message string or
        f-string.  A silent fallback has neither.
        """
        call = self._unwrapped_call(stmt)
        if call is None:
            return False
        name = _get_name(call.func)
        leaf = (name.rsplit(".", 1)[-1] if name else "").lstrip("_").lower()
        if not any(word in leaf for word in self._REPORTER_WORDS):
            return False
        return any(
            (isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value)
            or isinstance(arg, ast.JoinedStr)
            for arg in [*call.args, *(kw.value for kw in call.keywords)]
        )


def _raises_chain_caught(handler: ast.ExceptHandler) -> bool:
    """True when every ``raise ... from <cause>`` in *handler* chains the caught error.

    A bare ``raise`` or ``raise X(...)`` keeps the ImportError as implicit
    context. An explicit ``from`` replaces that context, so the cause must read
    the handler's bound name; an unbound handler has nothing to chain. Nested
    ``def``/``class`` bodies are skipped — their raises are not handler exits.
    """
    caught = frozenset({handler.name}) if handler.name else frozenset()
    for stmt in handler.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in (stmt, *_iter_shallow(stmt)):
            if not isinstance(node, ast.Raise) or node.cause is None:
                continue
            if not any(
                isinstance(n, ast.Name) and n.id in caught for n in ast.walk(node.cause)
            ):
                return False
    return True
