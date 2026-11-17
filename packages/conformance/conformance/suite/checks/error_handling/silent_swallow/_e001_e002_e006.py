"""E001 / E002 / E006 — bare and pass-only ``except`` handlers."""

from __future__ import annotations

import ast

from .._helpers import (
    _body_is_only_pass,
    _get_name,
)


class E001E002E006Mixin:
    """Rule methods for E001 / E002 / E006."""

    # ── E001 / E002 / E006 ───────────────────────────────────────────────────

    def _check_p001_p002_p006(self, node: ast.ExceptHandler) -> None:
        is_bare = node.type is None
        is_pass_only = _body_is_only_pass(node.body)

        if is_bare:
            if is_pass_only:
                self._add(
                    "E001",
                    node,
                    "Bare 'except: pass' silently discards every exception including "
                    "SystemExit and KeyboardInterrupt — the hardest class of bugs to debug. "
                    "Replace with a typed catch that at minimum logs at DEBUG.",
                )
            else:
                self._add(
                    "E006",
                    node,
                    "Bare 'except:' (no type) catches SystemExit and KeyboardInterrupt. "
                    "Use 'except Exception:' at minimum.",
                )
        elif is_pass_only:
            if isinstance(node.type, ast.Tuple):
                names = [_get_name(e) or "?" for e in node.type.elts]
                exc_type = "(" + ", ".join(names) + ")"
            else:
                exc_type = _get_name(node.type) or "Exception"
            self._add(
                "E002",
                node,
                f"'except {exc_type}: pass' silently discards the exception with no trace. "
                "Acceptable only for genuinely trivial best-effort operations — "
                "add a comment and log at DEBUG or use the suppression directive.",
            )
