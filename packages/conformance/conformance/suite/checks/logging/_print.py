"""L005 — print-in-production-code rule."""

from __future__ import annotations

import ast

from ._base import _MixinBase


class PrintMixin(_MixinBase):
    """Rule method for L005 (log-format category — print bypass)."""

    # ── L005 PrintInProductionCode ────────────────────────────────────────────

    def _check_l005(self, node: ast.Call) -> None:
        """Flag ``print()`` calls in production code.

        ``print()`` produces no level, no structured fields, no correlation IDs.
        In production services, output may go to stdout unformatted, be lost, or
        interleave with structured log lines.

        Test files and ``if __name__ == '__main__':`` blocks are already
        excluded by the discovery walk and the checker's ``_in_main_block``
        flag respectively.  CLI-script print() can be suppressed with a
        ``# conformance: ignore[L005] CLI output`` directive.
        """
        func = node.func
        if not isinstance(func, ast.Name):
            return
        if func.id != "print":
            return
        self._add(
            "L005",
            node,
            "print() in production code — bypasses the logging framework "
            "(no level, no correlation ID, no OTel forwarding). "
            "Replace with logger.debug() / logger.info().",
        )
