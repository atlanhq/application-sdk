"""Shared pytest fixtures for the .github/scripts test suite."""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip real backoff, but let the skipped time still COUNT.

    ``renovate_pkl_sync.regenerate`` retries ``pkl eval`` with a real
    ``time.sleep`` backoff (``EVAL_RETRY_SLEEP_S``). Tests that stub eval to fail
    would otherwise block for the full backoff on every attempt. That is what
    this started as: a sleep that vanished.

    A vanishing sleep is only safe for a loop bounded by an ATTEMPT COUNT. Every
    wait in these scripts that is bounded by a DEADLINE instead — ``while
    time.monotonic() < deadline`` — reads the real clock, so removing the sleep
    from between the iterations does not shorten the wait by one second. It just
    converts a patient poll into a busy spin that runs the loop body as fast as
    the interpreter can, for the full wall-clock budget. With ``capsys``
    capturing a progress line per iteration, that also buffers the spin's output
    in memory: one such test held 2.4 GB and ran for its entire 300s budget.

    So the stub advances a clock instead of discarding the sleep. ``sleep(10)``
    costs nothing and moves ``monotonic`` forward ten seconds, which is what the
    deadline loops are actually asking about — a 300s budget polled every 10s
    now ends after 30 iterations, instantly, having exercised the real loop and
    the real bound. Attempt-counted loops are unaffected.

    Patched on the stdlib module, as the string form it replaces was: every
    script here does ``import time`` and reads the attribute at call time. Safe
    to do globally in THIS suite because nothing in ``.github/scripts`` runs an
    event loop — asyncio's timers read the same ``time.monotonic``, and a clock
    that jumps would fire them early. Check that still holds before copying this
    fixture somewhere that does.
    """
    offset = 0.0
    real_monotonic = time.monotonic

    def _sleep(seconds: float = 0.0, *_a: object, **_k: object) -> None:
        nonlocal offset
        offset += max(float(seconds), 0.0)

    def _monotonic() -> float:
        return real_monotonic() + offset

    monkeypatch.setattr(time, "sleep", _sleep)
    monkeypatch.setattr(time, "monotonic", _monotonic)
