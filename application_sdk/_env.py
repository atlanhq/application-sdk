"""Tolerant env-var parsing: the one reader for every typed SDK env var.

Every SDK setting read from the environment goes through one of these
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

A warning alone would bypass OTel, and Python shows each one once per process.
So every default-path message is also handed to the SDK logger: the logger
module installs itself with :func:`set_log_sink` once it exists, and the
messages from before that (``constants``' import-time reads) are held until
then and logged in order.
"""

from __future__ import annotations

import math
import os
import re
import sys
import threading
import warnings
from collections.abc import Callable, Collection
from typing import TypeVar, cast, overload

#: Receives the message for a rejected value. ``None`` means ``warnings.warn``.
Reporter = Callable[[str], None]

_T = TypeVar("_T")

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


#: Default-path messages from before :func:`set_log_sink` ran, oldest first.
_pending: list[str] = []
_log_sink: Reporter | None = None
#: Held while reading the sink and holding a message, and while installing the
#: sink and draining, so a message held during installation is still drained.
_sink_lock = threading.Lock()


def set_log_sink(sink: Reporter) -> None:
    """Log every default-path rejection through *sink*, starting with the held ones.

    Called once by the SDK logger module when it is imported, so a malformed
    value read while ``constants`` was importing still reaches the logs.
    """
    global _log_sink
    with _sink_lock:
        _log_sink = sink
        held = list(_pending)
        _pending.clear()
    for message in held:
        sink(message)


def _to_log(message: str) -> None:
    with _sink_lock:
        sink = _log_sink
        if sink is None:
            _pending.append(message)
            return
    sink(message)


def warn_rejected(message: str, category: type[Warning] = RuntimeWarning) -> None:
    """Report a rejected setting the way the ``env_*`` readers do.

    For a module that parses a value itself: warns at the caller's caller (as
    ``warnings.warn(stacklevel=2)`` from the caller would) and logs it.
    """
    warnings.warn(message, category, stacklevel=3)
    _to_log(message)


def _reject(message: str, report: Reporter | None) -> None:
    if report is not None:
        report(message)
        return
    # stacklevel 3: point at the line that called env_int/env_float/..., not
    # at this helper or the env_* function itself.
    warnings.warn(message, RuntimeWarning, stacklevel=3)
    _to_log(message)


def _fallback(default: object) -> str:
    """The tail of a rejection message: what the reader returns instead."""
    if default is None:
        return "ignoring it"
    if isinstance(default, float):
        return f"using the default {default:g}"
    return f"using the default {default}"


@overload
def env_int(
    key: str,
    default: int = 0,
    *,
    minimum: int | None = None,
    report: Reporter | None = None,
) -> int: ...
@overload
def env_int(
    key: str,
    default: None,
    *,
    minimum: int | None = None,
    report: Reporter | None = None,
) -> int | None: ...
def env_int(
    key: str,
    default: int | None = 0,
    *,
    minimum: int | None = None,
    report: Reporter | None = None,
) -> int | None:
    """Read an integer env var.

    Returns *default* when the variable is unset or empty; pass ``None`` for a
    setting where "unset" means "no override". A value that is not an integer,
    or is below *minimum*, warns and returns *default*.
    """
    raw = _raw(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        _reject(f"{key}={raw!r} is not an integer; {_fallback(default)}", report)
        return default
    if minimum is not None and value < minimum:
        _reject(f"{key}={value} is below {minimum}; {_fallback(default)}", report)
        return default
    return value


@overload
def env_float(
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    report: Reporter | None = None,
) -> float: ...
@overload
def env_float(
    key: str,
    default: None,
    *,
    minimum: float | None = None,
    report: Reporter | None = None,
) -> float | None: ...
def env_float(
    key: str,
    default: float | None,
    *,
    minimum: float | None = None,
    report: Reporter | None = None,
) -> float | None:
    """Read a finite float env var.

    Returns *default* when the variable is unset or empty; pass ``None`` for a
    setting where "unset" means "no override". A value that is not a number, is
    not finite (``inf`` / ``nan``), or is below *minimum*, warns and returns
    *default*. Non-finite values are rejected because an ``inf`` bound never
    trips and every ``nan`` comparison is False, so either one silently
    disables whatever the value bounds.
    """
    raw = _raw(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        _reject(f"{key}={raw!r} is not a number; {_fallback(default)}", report)
        return default
    if not math.isfinite(value):
        _reject(f"{key}={raw!r} is not finite; {_fallback(default)}", report)
        return default
    if minimum is not None and value < minimum:
        _reject(f"{key}={value:g} is below {minimum:g}; {_fallback(default)}", report)
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


def env_str(key: str, default: str) -> str:
    """Read a string env var.

    Returns the value with surrounding whitespace stripped, or *default* when
    the variable is unset or empty. ``os.getenv(key, default)`` returns ``""``
    for an empty value, so an unset Helm value would replace the default with
    an empty string; this reader keeps the default.
    """
    raw = _raw(key)
    return default if raw is None else raw


#: Units the object-store client's duration parser (Rust ``humantime`` 2.3)
#: accepts. Case-sensitive: ``M`` is months and ``m`` is minutes.
_DURATION_UNITS = (
    "nanos|nsec|ns|usec|us|µs|millis|msec|ms|seconds|second|secs|sec|s|"
    "minutes|minute|mins|min|m|hours|hour|hrs|hr|h|days|day|d|"
    "weeks|week|wks|wk|w|months|month|M|years|year|yrs|yr|y"
)
#: ASCII digits only: ``\d`` also matches other scripts' digits (``١``), which
#: obstore rejects. Whitespace stays Unicode-aware, as obstore's is.
_DURATION = re.compile(rf"(?:[0-9]+(?:\.[0-9]+)?\s*(?:{_DURATION_UNITS})\s*)+")


def is_duration(value: str) -> bool:
    """True when *value* is a duration the object-store client parses.

    One or more ``<number><unit>`` terms (``30s``, ``1h 30m``, ``1.5h``), or
    ``0``. A bare number has no unit and is rejected, as the client rejects it.
    """
    stripped = value.strip()
    return stripped == "0" or _DURATION.fullmatch(stripped) is not None


def env_duration(key: str, default: str, *, report: Reporter | None = None) -> str:
    """Read a duration env var such as ``30s`` or ``1h 30m``; see :func:`is_duration`.

    Returns *default* when the variable is unset or empty. A value that is not a
    duration warns and returns *default*: passed through, it would fail every
    object-store client construction with a config error.
    """
    raw = _raw(key)
    if raw is None:
        return default
    if is_duration(raw):
        return raw
    _reject(
        f"{key}={raw!r} is not a duration such as '30s' or '1h 30m'; "
        f"using the default {default!r}",
        report,
    )
    return default


__all__ = [
    "FALSE_VALUES",
    "LOCAL_DEPLOYMENT_NAME",
    "Reporter",
    "TRUE_VALUES",
    "dapr_components_path",
    "deployment_name",
    "env_bool",
    "env_choice",
    "env_duration",
    "env_float",
    "env_int",
    "env_str",
    "is_duration",
    "pod_name",
]


#: The module whose import-time settings :func:`sdk_constant` defers to.
_CONSTANTS_MODULE = "application_sdk.constants"


def sdk_constant(name: str, read: Callable[[], _T]) -> _T:
    """``application_sdk.constants.<name>`` once that module is loaded, else ``read()``.

    ``constants`` reads every setting once, at import, and imports
    python-dotenv to load ``./.env`` first. The worker always has it loaded,
    and tests and the SDK's testing fixtures patch its attributes
    (``TEMPORARY_PATH`` above all), so on the worker its value is the one in
    force. The API host ships the object store's read and write path
    (``application_sdk.storage.ops`` and what it needs) without python-dotenv,
    so those modules must not import ``constants``. They read a setting
    through here instead: the worker's value whenever ``constants`` is
    loaded, else *read*, which reads the same variable with the same default.
    ``tests/unit/test_api_distribution.py`` pins that each reader agrees with
    ``constants``.
    """
    module = sys.modules.get(_CONSTANTS_MODULE)
    if module is not None:
        return cast("_T", getattr(module, name))
    return read()


def temporary_path() -> str:
    """The SDK's local staging root (``ATLAN_TEMPORARY_PATH``, default ``./local/tmp/``).

    ``application_sdk.constants.TEMPORARY_PATH`` when that module is loaded
    (see :func:`sdk_constant`).
    """
    return sdk_constant(
        "TEMPORARY_PATH", lambda: env_str("ATLAN_TEMPORARY_PATH", "./local/tmp/")
    )


#: The identity a process carries when ``ATLAN_DEPLOYMENT_NAME`` is unset:
#: local development. ``application_sdk.constants.LOCAL_ENVIRONMENT`` is this
#: value.
LOCAL_DEPLOYMENT_NAME = "local"


def deployment_name() -> str:
    """``ATLAN_DEPLOYMENT_NAME`` as an identity, ``"local"`` when unset or empty.

    The one default for the variable wherever it names this process: object-store
    prefixes, log tagging, the worker-start and token-refresh events. Temporal
    queue derivation does not default it: it reads the raw value through
    :func:`application_sdk.common.task_queue.deployment_name_from_env`, where
    unset means "no deployment prefix", not ``"local"``.
    """
    return env_str("ATLAN_DEPLOYMENT_NAME", LOCAL_DEPLOYMENT_NAME)


def dapr_components_path() -> str:
    """The directory of Dapr component YAMLs (``DAPR_COMPONENTS_PATH``, default ``./components``).

    The shipped image's ``entrypoint.sh`` always exports the variable
    (``/app/components``, the image's working directory), and
    ``application_sdk.dev.embedded_dapr`` sets it for local runs, so the default
    applies only to a process started some other way from the app's root.
    """
    return env_str("DAPR_COMPONENTS_PATH", "./components")


def pod_name() -> str:
    """This process's pod name: ``K8S_POD_NAME``, else ``HOSTNAME``, else ``""``.

    ``HOSTNAME`` is the kubelet's own pod-name variable, so a pod without the
    downward-API ``K8S_POD_NAME`` still names itself the same way everywhere.
    """
    return env_str("K8S_POD_NAME", "") or env_str("HOSTNAME", "")
