"""Recognition of redaction/sanitizer helpers — deliberate no-traceback boundaries.

Several fleet apps deliberately omit ``exc_info=True`` on except-block logs and
instead format the exception through a redaction helper, because the raw
driver/API exception (and therefore its traceback) can embed credentials:
JDBC URLs carrying passwords, ``Authorization`` headers/HMACs, connection
properties, OAuth response bodies.  Typical shapes::

    logger.warning("close failed: %s", redact(e))
    logger.error("auth failed: %s\\n%s", sanitize_cause_repr(e), safe_traceback(e))

Demanding ``exc_info=True`` at such a site (L004/E005) is anti-security: the
separately-serialized traceback bypasses the redaction the code already
performs.  A production security review over the fleet remediation (FND-57)
confirmed this pattern in five connector repos.

This module is the shared detector both series use to exempt those sites.
Recognition is textual-by-name on purpose: the helpers live in each app, so
the checker cannot resolve them — but the naming is the documented convention.
"""

from __future__ import annotations

import ast

#: Substrings that mark a callable or variable as a redaction helper.  Matched
#: case-insensitively against simple names (``redact_secrets``) and attribute
#: leaves (``utils.redact``).  ``sanitiz`` covers sanitize/sanitizer/sanitised.
SANITIZER_NAME_WORDS: tuple[str, ...] = (
    "redact",
    "sanitiz",
    "scrub_secret",
    "safe_traceback",
    "mask_secret",
)


def _name_is_sanitizer(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in SANITIZER_NAME_WORDS)


def _leaf_name(expr: ast.expr) -> str | None:
    """Return the identifier a call target or variable resolves to, if simple."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def call_uses_sanitizer(call: ast.Call) -> bool:
    """True when any argument of *call* flows through a recognised sanitizer.

    Two shapes count:

    * a direct helper call among the arguments — ``redact(e)``,
      ``redact_secrets(str(e))``, ``errors.sanitize_cause_repr(e)``;
    * a variable argument whose *name* marks it as pre-sanitised —
      ``safe_traceback`` in ``logger.error("…%s", safe_traceback)`` where the
      redacted text was built on a previous line.

    Only the log call's own arguments are inspected (positional and keyword,
    including nested expressions) — a sanitizer used elsewhere in the handler
    does not exempt an unrelated log call.
    """
    for arg in [*call.args, *[kw.value for kw in call.keywords]]:
        for node in ast.walk(arg):
            if isinstance(node, ast.Call):
                target = _leaf_name(node.func)
                if target is not None and _name_is_sanitizer(target):
                    return True
            elif isinstance(node, ast.Name) and _name_is_sanitizer(node.id):
                return True
    return False
