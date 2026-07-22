"""Shared pytest fixtures for the .github/scripts test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the pkl-eval retry backoff so eval-failure tests don't sleep.

    ``renovate_pkl_sync.regenerate`` retries ``pkl eval`` with a real
    ``time.sleep`` backoff (``EVAL_RETRY_SLEEP_S``). Tests that stub eval to fail
    would otherwise block for the full backoff on every attempt.
    """
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
