"""E003 — ``contextlib.suppress`` of a broad exception type."""

from __future__ import annotations

import ast

from .._constants import _BROAD_EXCEPT_TYPES
from .._helpers import (
    _get_name,
)


class E003Mixin:
    """Rule methods for E003."""

    # ── E003 ─────────────────────────────────────────────────────────────────

    def _check_p003(self, node: ast.Call) -> None:
        name = _get_name(node.func)
        if name != "suppress":
            return
        for arg in node.args:
            arg_name = _get_name(arg)
            if arg_name in _BROAD_EXCEPT_TYPES:
                self._add(
                    "E003",
                    node,
                    f"contextlib.suppress({arg_name}) — scope is too broad; "
                    f"suppresses every exception. Use a specific exception type "
                    f"(e.g. suppress(FileNotFoundError)).",
                )
                return
