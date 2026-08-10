"""L010 — credential-in-log security rule."""

from __future__ import annotations

import ast

from ._base import _MixinBase
from ._helpers import _is_credential_value_name, is_logger_call


class SecurityMixin(_MixinBase):
    """Rule method for L010 (security category)."""

    # ── L010 CredentialInLogOutput ────────────────────────────────────────────

    def _check_l010(self, node: ast.Call) -> None:
        """Flag logger calls that appear to log a credential *value*.

        Detection is intentionally narrow to minimise false positives — BLOCK
        tier findings in CI will break builds.  Two signals are checked:

        1. A keyword argument whose name matches a credential-value suffix
           (``password=``, ``token=``, etc.) but does NOT end with a label
           suffix (``_name``, ``_type``, ``_id``).

        2. A non-string, non-None positional argument (a Name or Attribute
           node) whose identifier matches the credential-value heuristic —
           e.g. ``logger.error("auth failed", password)`` where ``password``
           is passed as a format arg.

        Names assigned a redaction placeholder in the module (e.g.
        ``password = "[REDACTED]" if creds.get("password") else None``) are
        exempt — logging them is a presence indicator, not a value leak.

        Logging a credential *name* (``token_name=``) is acceptable.
        Logging a credential *value* is a security vulnerability.
        Requires human review — never auto-fixed.
        """
        if not is_logger_call(node, self._logging_module_names):
            return

        # Check 1 — keyword arguments
        for kw in node.keywords:
            if kw.arg is None:
                continue
            if _is_credential_value_name(kw.arg):
                if (
                    isinstance(kw.value, ast.Name)
                    and kw.value.id in self._redacted_names
                ):
                    continue  # value is a redaction placeholder — presence indicator only
                self._add(
                    "L010",
                    node,
                    f"Credential value may be in log output (kwarg '{kw.arg}'). "
                    "Log the credential *name* or *type*, not its value. "
                    "Requires security review — suppress with justification if safe.",
                )
                return  # one finding per call site is enough

        # Check 2 — positional arguments after the format string
        # Skip args[0] (the message/format string itself)
        for arg in node.args[1:]:
            name: str | None = None
            if isinstance(arg, ast.Name):
                name = arg.id
            elif isinstance(arg, ast.Attribute):
                name = arg.attr
            if name and _is_credential_value_name(name):
                if name in self._redacted_names:
                    continue  # assigned "[REDACTED]"/None upstream — presence indicator only
                self._add(
                    "L010",
                    node,
                    f"Credential value may be in log output (argument '{name}'). "
                    "Log the credential *name* or *type*, not its value. "
                    "Requires security review — suppress with justification if safe.",
                )
                return
