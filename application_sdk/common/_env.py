"""Env-var parsing for SDK modules that run after logging is available.

Same rules as :mod:`application_sdk._env` (unset or empty is the default; a
malformed value is the default, reported; one boolean vocabulary), but a
rejected value is reported as a WARNING through the SDK logger, so it reaches
the same pipeline as every other runtime log line.

``application_sdk.constants`` and ``application_sdk._runtime`` cannot use this
module: importing it runs ``application_sdk.common.__init__``, which imports
``constants``. They read through :mod:`application_sdk._env` directly.
"""

from collections.abc import Collection
from typing import overload

from application_sdk import _env
from application_sdk._env import FALSE_VALUES as FALSE_VALUES
from application_sdk._env import TRUE_VALUES as TRUE_VALUES
from application_sdk._env import env_str as env_str
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def _report(message: str) -> None:
    logger.warning("Ignoring env var: %s", message)


@overload
def env_int(key: str, default: int = 0, *, minimum: int | None = None) -> int: ...
@overload
def env_int(key: str, default: None, *, minimum: int | None = None) -> int | None: ...
def env_int(
    key: str, default: int | None = 0, *, minimum: int | None = None
) -> int | None:
    """Read an int env var; see :func:`application_sdk._env.env_int`.

    A malformed value like ``ATLAN_HANDLER_PORT="not-a-number"`` falls through
    to the default with a warning instead of crashing startup.
    """
    return _env.env_int(key, default, minimum=minimum, report=_report)


@overload
def env_float(key: str, default: float, *, minimum: float | None = None) -> float: ...
@overload
def env_float(
    key: str, default: None, *, minimum: float | None = None
) -> float | None: ...
def env_float(
    key: str, default: float | None, *, minimum: float | None = None
) -> float | None:
    """Read a finite float env var; see :func:`application_sdk._env.env_float`."""
    return _env.env_float(key, default, minimum=minimum, report=_report)


def env_duration(key: str, default: str) -> str:
    """Read a duration env var; see :func:`application_sdk._env.env_duration`."""
    return _env.env_duration(key, default, report=_report)


def env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean env var; see :func:`application_sdk._env.env_bool`."""
    return _env.env_bool(key, default, report=_report)


def env_choice(key: str, allowed: Collection[str], default: str) -> str:
    """Read a one-of-these-words env var; see :func:`application_sdk._env.env_choice`."""
    return _env.env_choice(key, allowed, default, report=_report)
