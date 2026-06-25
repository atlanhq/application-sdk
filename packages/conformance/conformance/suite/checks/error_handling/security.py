"""E017 — secret-in-error-evidence security rule."""

from __future__ import annotations

import ast

from ._constants import _SECRET_SUFFIXES, LEAF_CLASSES
from ._helpers import _get_name


class SecurityMixin:
    """Rule methods for E017 (security category)."""

    # ── E017 ─────────────────────────────────────────────────────────────────

    def _check_p017_raise(self, node: ast.Raise) -> None:
        if node.exc is None or not isinstance(node.exc, ast.Call):
            return
        for kw in node.exc.keywords:
            if kw.arg and any(kw.arg.endswith(s) for s in _SECRET_SUFFIXES):
                self._add(
                    "E017",
                    node,
                    f"Evidence key '{kw.arg}' ends in a secret suffix — "
                    f"the wire layer rejects this at runtime (ValueError). "
                    f"Rename to a safe key (e.g. credential_name, token_type).",
                )
                return

    def _check_p017_call(self, node: ast.Call) -> None:
        """Catch construct-then-raise: ``err = SomeError(api_secret=...); raise err``."""
        # Skip when we are already inside a Raise node — _check_p017_raise
        # covers that case on the outer Raise and firing again on the inner
        # Call would double-report the same site.
        if self._in_raise_call:
            return
        func_name = _get_name(node.func)
        if func_name not in LEAF_CLASSES:
            return
        for kw in node.keywords:
            if kw.arg and any(kw.arg.endswith(s) for s in _SECRET_SUFFIXES):
                self._add(
                    "E017",
                    node,
                    f"Evidence key '{kw.arg}' ends in a secret suffix — "
                    f"the wire layer rejects this at runtime (ValueError). "
                    f"Rename to a safe key (e.g. credential_name, token_type).",
                )
                return
