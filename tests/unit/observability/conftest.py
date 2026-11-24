"""Shared fixtures for the observability unit tests."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest


@pytest.fixture
def non_utc_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the test with the process local timezone at UTC+05:30.

    Pods run in UTC, where a naive local reading and a UTC one agree; only a
    host in another zone (SDR on customer infrastructure, a laptop) tells them
    apart. Asia/Kolkata has no DST, so the offset is the same all year.
    ``time.tzset`` is POSIX-only; the Windows leg skips.
    """
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only")
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()
