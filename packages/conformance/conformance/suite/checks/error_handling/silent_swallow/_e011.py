"""E011 — a ``logging.Filter`` that swallows exceptions."""

from __future__ import annotations

import ast

from .._helpers import (
    _filter_body_wrapped,
    _find_filter_method,
    _inherits_logging_filter,
)


class E011Mixin:
    """Rule methods for E011."""

    # ── E011 ─────────────────────────────────────────────────────────────────

    def _check_p011(self, cls: ast.ClassDef) -> None:
        if not _inherits_logging_filter(cls):
            return
        method = _find_filter_method(cls)
        if method is None:
            return
        if not _filter_body_wrapped(method):
            self._add(
                "E011",
                method,
                f"logging.Filter.filter() body in '{cls.name}' is not fully wrapped in "
                f"try/except — an unguarded exception crashes the logging caller "
                f"(Logger.handle() has no try/except around filters). "
                f"Wrap the entire body and return a safe fallback (True = pass-through).",
            )
