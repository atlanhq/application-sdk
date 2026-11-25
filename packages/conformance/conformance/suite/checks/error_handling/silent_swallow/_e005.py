"""E005 — an exception logged without its traceback."""

from __future__ import annotations

import ast

from ..._ast_common._sanitizers import (
    call_uses_sanitizer,
)
from .._helpers import (
    _has_exc_info,
    _iter_shallow,
)


class E005Mixin:
    """Rule methods for E005."""

    # ── E005 ─────────────────────────────────────────────────────────────────

    def _check_p005(self, node: ast.ExceptHandler) -> None:
        for n in _iter_shallow(node):
            if not isinstance(n, ast.Expr):
                continue
            call = n.value
            if isinstance(call, ast.Await):
                call = call.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr == "exception":
                continue  # logger.exception() implies exc_info — skip
            if call_uses_sanitizer(call, handler=node):
                # Deliberate redaction boundary — exc_info would serialize the
                # raw exception past the sanitizer and can leak credentials.
                continue
            if func.attr in ("warning", "error", "critical") and not _has_exc_info(
                call, node.name
            ):
                self._add(
                    "E005",
                    n,
                    f"logger.{func.attr}() inside except block is missing exc_info=True — "
                    f"the stack trace is silently discarded. Add exc_info=True.",
                )
