"""Tolerant env-var parsing: the one reader for every typed SDK env var.

Every SDK setting read from the environment goes through one of these four
functions, so they all share one set of rules:

* **Unset or empty is the default.** An unset Helm value renders as ``""``, so
  an empty string means "not configured", never "zero" or "false".
* **A malformed value is the default, with a warning.** A typo must not stop
  every process at import, before logging is configured. The value falls back
  to the default and a message names the variable, the rejected value and
  the default used instead.
* **One boolean vocabulary.** ``true`` / ``1`` / ``yes`` / ``on`` enable and
  ``false`` / ``0`` / ``no`` / ``off`` disable, case-insensitive with surrounding
  whitespace ignored. Any other word warns and keeps the default.

Standard library only, and deliberately outside ``application_sdk.common``:
``application_sdk.constants`` and ``application_sdk._runtime`` read through it
at import time. Importing anything under ``common`` would run that package's
``__init__``, which imports ``constants``, which is a cycle. For the same reason
the message goes to :mod:`warnings` as a ``RuntimeWarning`` by default rather
than to the SDK logger: the logger imports ``constants``, and at import time
logging is not configured yet. A caller that runs after logging is up passes
``report`` to route the message to its logger instead;
:mod:`application_sdk.common._env` does that for SDK modules that can import the
logger.
"""

from __future__ import annotations

import math
import os
import warnings
from collections.abc import Callable, Collection

#: Receives the message for a rejected value. ``None`` means ``warnings.warn``.
Reporter = Callable[[str], None]

#: Words that switch a boolean env var on.
TRUE_VALUES: frozenset[str] = frozenset({"true", "1", "yes", "on"})
#: Words that switch a boolean env var off.
FALSE_VALUES: frozenset[str] = frozenset({"false", "0", "no", "off"})


def _raw(key: str) -> str | None:
    """The stripped value of *key*, or ``None`` when unset or empty."""
    value = os.environ.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _reject(message: str, report: Reporter | None) -> None:
    if report is not None:
        report(message)
        return
    # stacklevel 3: point at the line that called env_int/env_float/..., not
    # at this helper or the env_* function itself.
    warnings.warn(message, RuntimeWarning, stacklevel=3)


def env_int(
    key: str,
    default: int = 0,
    *,
    minimum: int | None = None,
    report: Reporter | None = None,
) -> int:
    """Read an integer env var.

    Returns *default* when the variable is unset or empty. A value that is not
    an integer, or is below *minimum*, warns and returns *default*.
    """
    raw = _raw(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _reject(f"{key}={raw!r} is not an integer; using the default {default}", report)
        return default
    if minimum is not None and value < minimum:
        _reject(
            f"{key}={value} is below {minimum}; using the default {default}", report
        )
        return default
    return value


def env_float(
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    report: Reporter | None = None,
) -> float:
    """Read a finite float env var.

    Returns *default* when the variable is unset or empty. A value that is not a
    number, is not finite (``inf`` / ``nan``), or is below *minimum*, warns and
    returns *default*. Non-finite values are rejected because an ``inf`` bound
    never trips and every ``nan`` comparison is False, so either one silently
    disables whatever the value bounds.
    """
    raw = _raw(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        _reject(f"{key}={raw!r} is not a number; using the default {default:g}", report)
        return default
    if not math.isfinite(value):
        _reject(f"{key}={raw!r} is not finite; using the default {default:g}", report)
        return default
    if minimum is not None and value < minimum:
        _reject(
            f"{key}={value:g} is below {minimum:g}; using the default {default:g}",
            report,
        )
        return default
    return value


def env_bool(
    key: str, default: bool = False, *, report: Reporter | None = None
) -> bool:
    """Read a boolean env var using :data:`TRUE_VALUES` / :data:`FALSE_VALUES`.

    Returns *default* when the variable is unset or empty. Any other word warns
    and returns *default*.
    """
    raw = _raw(key)
    if raw is None:
        return default
    word = raw.lower()
    if word in TRUE_VALUES:
        return True
    if word in FALSE_VALUES:
        return False
    _reject(
        f"{key}={raw!r} is not one of {sorted(TRUE_VALUES | FALSE_VALUES)}; "
        f"using the default {str(default).lower()}",
        report,
    )
    return default


def env_choice(
    key: str,
    allowed: Collection[str],
    default: str,
    *,
    report: Reporter | None = None,
) -> str:
    """Read an env var whose value must be one of *allowed* (lowercase words).

    The value is lowercased and stripped. Returns *default* when the variable
    is unset or empty. A word outside *allowed* warns and returns *default*,
    so a typo can never reach the code that branches on it and select whichever
    branch happens to be the ``else``.
    """
    raw = _raw(key)
    if raw is None:
        return default
    word = raw.lower()
    if word in allowed:
        return word
    _reject(f"{key}={raw!r} is not one of {sorted(allowed)}; using {default!r}", report)
    return default


__all__ = [
    "FALSE_VALUES",
    "Reporter",
    "TRUE_VALUES",
    "env_bool",
    "env_choice",
    "env_float",
    "env_int",
]
