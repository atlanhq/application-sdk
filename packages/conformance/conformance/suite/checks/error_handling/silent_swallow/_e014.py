"""E014 — ``continue`` / ``break`` / ``pass`` swallowing an exception inside a loop."""

from __future__ import annotations

import ast

from .._helpers import (
    _body_is_only_loop_control_no_logging,
    _get_name,
)


class E014Mixin:
    """Rule methods for E014."""

    # ── E014 ─────────────────────────────────────────────────────────────────

    def _check_p014(self, node: ast.ExceptHandler) -> None:
        if not self._loop_stack:
            return
        if not _body_is_only_loop_control_no_logging(node.body):
            return
        exc_type = _get_name(node.type) if node.type else "(bare)"
        self._add(
            "E014",
            node,
            f"except {exc_type}: [continue/break/pass] inside a loop — exception is "
            f"silently swallowed. Log at DEBUG before the loop control statement.",
        )
