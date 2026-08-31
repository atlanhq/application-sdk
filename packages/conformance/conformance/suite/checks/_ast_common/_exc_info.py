"""Recognition of ``exc_info`` values that actually carry a traceback.

``exc_info`` is not a boolean flag.  ``logging`` treats any truthy value as a
request to attach a traceback, and an exception *instance* is the documented
second form::

    except Exception as exc:
        logger.error("auth failed", exc_info=exc)   # same traceback as True

Both the L-series (L004) and the E-series (E004/E005) need this predicate, and
having it implemented twice is how ``exc_info=exc`` came to be silent for one
series and a finding for the other.  This module is the single definition both
consume — the same arrangement as :mod:`_sanitizers`.

Only the name bound by the surrounding ``except ... as`` clause counts.  An
arbitrary ``exc_info=some_other_name`` is not provably the live exception, so
it is deliberately not recognised.
"""

from __future__ import annotations

import ast


def has_exc_info_traceback(call: ast.Call, exception_name: str | None = None) -> bool:
    """True if *call* passes an ``exc_info`` value that carries a traceback.

    Recognises the literal ``exc_info=True`` and, when *exception_name* is the
    ``except ... as <name>`` binding in scope, ``exc_info=<name>``.  Any other
    ``exc_info`` value (``False``, ``None``, a call, an unrelated name) is
    reported as not carrying a traceback, and an absent keyword is likewise
    False.  The bound-name check is intentionally flow-insensitive: a name
    rebound after the ``except`` binding is still treated as that binding.
    """
    for kw in call.keywords:
        if kw.arg != "exc_info":
            continue
        val = kw.value
        if isinstance(val, ast.Constant) and val.value is True:
            return True
        if exception_name is not None and isinstance(val, ast.Name):
            return val.id == exception_name
        return False
    return False
