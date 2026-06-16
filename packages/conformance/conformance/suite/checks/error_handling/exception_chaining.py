"""E015, E016 — exception-chaining and message-hygiene rules."""

from __future__ import annotations

import ast

from ._helpers import _get_name, _message_kw_has_exc_text


class ExceptionChainingMixin:
    """Rule methods for E015 and E016 (exception-chaining category)."""

    # ── E015 ─────────────────────────────────────────────────────────────────

    def _check_p015(self, node: ast.Raise) -> None:
        if node.exc is None or not isinstance(node.exc, ast.Call):
            return
        exc_binding: str | None = None
        if self._except_stack:
            exc_binding = self._except_stack[-1].name
        for kw in node.exc.keywords:
            if kw.arg == "message" and _message_kw_has_exc_text(kw.value, exc_binding):
                exc_type = _get_name(node.exc.func) or "Error"
                self._add(
                    "E015",
                    node,
                    f"message= on {exc_type} contains interpolated exception text "
                    f"(f-string/str(exc)/repr(exc)) — leaks unsanitised text and breaks "
                    f"dashboard grouping. Put context in a typed evidence field instead.",
                )
                return

    # ── E016 ─────────────────────────────────────────────────────────────────

    def _check_p016(self, node: ast.Raise) -> None:
        if node.exc is None:
            return  # bare re-raise — always acceptable
        if node.cause is not None:
            return  # has 'from X' or 'from None' — acceptable
        if not self._except_stack:
            return
        handler = self._except_stack[-1]
        if handler.name is None:
            return  # no binding (except SomeError:) — chaining not applicable
        exc_binding = handler.name
        exc_name = _get_name(node.exc) or "NewError"
        self._add(
            "E016",
            node,
            f"raise {exc_name} inside 'except ... as {exc_binding}:' is missing "
            f"'from {exc_binding}'. The original exception is lost in AE dashboards. "
            f"Use 'raise {exc_name}(...) from {exc_binding}'.",
        )
