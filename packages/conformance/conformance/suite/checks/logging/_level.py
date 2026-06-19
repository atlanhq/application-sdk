"""L006, L007, L017 — log-level rules."""

from __future__ import annotations

import ast

from ._base import _MixinBase
from ._helpers import get_logger_method, loop_is_bounded


class LevelMixin(_MixinBase):
    """Rule methods for L006, L007, L017 (log-level category)."""

    # ── L006 InfoInTightLoop ──────────────────────────────────────────────────

    def _check_l006(self, node: ast.Call) -> None:
        """Flag ``logger.info()`` inside a loop that is not clearly bounded.

        Per-item INFO in a large loop emits O(N) records at the level operators
        monitor, drowning lifecycle signals in noise and inflating storage cost.
        INFO is for milestones; per-item progress belongs at DEBUG.

        Exempt: loops bounded to ≤ 10 iterations (literal range / collection).
        """
        if not self._loop_stack:
            return
        if get_logger_method(node, self._logging_module_names) != "info":
            return
        # Check if the innermost loop is clearly bounded
        innermost = self._loop_stack[-1]
        if isinstance(innermost, ast.While):
            # While loops are always flagged — bound is not statically obvious
            pass
        elif loop_is_bounded(innermost):  # type: ignore[arg-type]
            return
        self._add(
            "L006",
            node,
            "logger.info() inside a loop — generates excessive log volume. "
            "Use DEBUG per-item and INFO for the loop summary.",
        )

    # ── L007 LoggerCriticalUsage ──────────────────────────────────────────────

    def _check_l007(self, node: ast.Call) -> None:
        """Flag ``logger.critical(...)``.

        ADR-0011 codifies exactly four levels (DEBUG/INFO/WARNING/ERROR).
        Fatal conditions are communicated through process exit codes and Temporal
        workflow failure — use ERROR (with exc_info=True) instead.
        """
        if get_logger_method(node, self._logging_module_names) != "critical":
            return
        self._add(
            "L007",
            node,
            "logger.critical() — CRITICAL is not a sanctioned level (ADR-0011). "
            "Use logger.error(..., exc_info=True) and let the failure propagate.",
        )

    # ── L017 LoggerExceptionUsage ─────────────────────────────────────────────

    def _check_l017(self, node: ast.Call) -> None:
        """Flag ``logger.exception(...)``.

        ADR-0011 restricts app logging to four levels with ``exc_info=True`` as
        the sanctioned way to attach a traceback.  ``logger.exception()`` reads
        ``sys.exc_info()`` implicitly — capturing nothing (or a stale exception)
        outside an active except block.

        Exempt: files that define ``AtlanLoggerAdapter`` or ``get_logger`` at
        module top-level (the logging infrastructure itself).  The exemption is
        whole-file, not method-scoped — see ``is_adapter_file`` in ``_helpers.py``.
        """
        if self._is_adapter_file:
            return
        if get_logger_method(node, self._logging_module_names) == "exception":
            self._add(
                "L017",
                node,
                "logger.exception() is not a sanctioned method (ADR-0011). "
                "Replace with logger.error(..., exc_info=True).",
            )
