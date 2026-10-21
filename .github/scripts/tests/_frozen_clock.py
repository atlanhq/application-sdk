"""Pin a script module's wall clock for a test.

A test that computes "today" itself and then asserts on a script's "today" reads
the clock twice. Across a midnight rollover the two disagree, so the test fails
for a few seconds a day (and for hours in a non-UTC local zone when one side
uses UTC). Freezing the module's clock to the instant the test also uses makes
the two the same value by construction.

Patches whichever of ``datetime`` / ``date`` the module imported by name
(``from datetime import date, datetime``), and leaves the rest of the class
untouched, so ``fromisoformat``, arithmetic and comparisons behave as usual.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import ModuleType

import pytest


def freeze_clock(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, at: datetime
) -> datetime:
    """Make ``module``'s ``datetime.now()`` / ``date.today()`` return ``at``.

    ``at`` must be timezone-aware. ``now(tz)`` converts it to ``tz``; a naive
    ``now()`` / ``today()`` returns its wall-clock fields with no tzinfo.
    Returns ``at`` so callers can derive their expected values from it.
    """
    if at.tzinfo is None:
        raise ValueError("freeze_clock needs a timezone-aware datetime")

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return at.astimezone(tz) if tz is not None else at.replace(tzinfo=None)

        @classmethod
        def today(cls):  # type: ignore[override]
            return at.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):  # type: ignore[override]
            return at.astimezone(timezone.utc).replace(tzinfo=None)

    class _FrozenDate(date):
        @classmethod
        def today(cls):  # type: ignore[override]
            return date(at.year, at.month, at.day)

    patched = False
    if getattr(module, "datetime", None) is datetime:
        monkeypatch.setattr(module, "datetime", _FrozenDateTime)
        patched = True
    if getattr(module, "date", None) is date:
        monkeypatch.setattr(module, "date", _FrozenDate)
        patched = True
    if not patched:
        raise AssertionError(
            f"{module.__name__} imports neither `datetime` nor `date` by name; "
            "nothing to freeze"
        )
    return at
