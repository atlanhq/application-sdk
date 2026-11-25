"""Coverage must never fail or change the step it observes.

Every piece of coverage code that runs inside an app's lineage code goes through
one of these. The first error stops recording for the rest of the run (the
recorder's ``enabled`` goes false and ``error`` names the exception type), warns
once, and is swallowed. Only ``Exception`` is caught: cancellation and
interpreter exits pass through untouched.

A guard wraps coverage statements only. Lineage code stays outside it, so its
errors fail the step exactly as they do without coverage.
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Callable
from types import TracebackType
from typing import Any, ParamSpec, TypeVar

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

__all__ = ["CoverageGuard", "guarded", "stop_recording"]


def stop_recording(owner: Any, exc: BaseException) -> None:
    """Turn recording off on ``owner`` after a coverage error. Never raises.

    ``owner`` is anything with ``enabled`` and ``error`` attributes (a recorder),
    or ``None`` when the failing code has no recorder to turn off.
    """
    # The guard itself must not raise.
    with contextlib.suppress(Exception):
        first = owner is None or getattr(owner, "error", None) is None
        if owner is not None:
            owner.enabled = False
            if first:
                owner.error = type(exc).__name__
        if first:
            logger.warning(
                "Lineage coverage stopped recording after an error; lineage is unaffected",
                error_type=type(exc).__name__,
            )


class CoverageGuard:
    """``with CoverageGuard(recorder):`` around coverage-only statements.

    Do not ``return``, ``break``, ``continue`` or ``yield`` inside the block, and
    do not bind a name there that lineage code reads afterwards: a swallowed
    error would leave it unbound.
    """

    __slots__ = ("_owner",)

    def __init__(self, owner: Any = None) -> None:
        self._owner = owner

    def __enter__(self) -> CoverageGuard:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is None:
            return False
        if not isinstance(exc, Exception):
            return False
        stop_recording(self._owner, exc)
        return True


def guarded(method: Callable[P, R]) -> Callable[P, R | None]:
    """Method decorator: skip when recording is off, swallow and stop on error."""

    @functools.wraps(method)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        owner = args[0] if args else None
        if not getattr(owner, "enabled", True):
            return None
        try:
            return method(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - coverage must not fail the step
            stop_recording(owner, exc)
            return None

    return wrapper
