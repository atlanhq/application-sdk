"""Tests for .github/scripts/warm_embedded_runtimes.py and the cache it seeds."""

from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import warm_embedded_runtimes as warm

_REPO = Path(__file__).resolve().parents[3]
_CACHE_ACTION = "./.github/actions/embedded-runtimes-cache"


def _run_to_completion(coro) -> None:
    """Drive a coroutine that never suspends, without an event loop.

    This suite patches ``time`` globally (see conftest.py), which is only safe
    while nothing here runs an event loop.
    """
    with pytest.raises(StopIteration):
        coro.send(None)


def test_warm_enters_and_leaves_both_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    def _tracking(name: str):
        @asynccontextmanager
        async def _ctx():
            events.append(f"enter {name}")
            yield object()
            events.append(f"exit {name}")

        return _ctx

    fake = types.ModuleType("application_sdk.dev")
    fake.embedded_runtime = _tracking("temporal")  # type: ignore[attr-defined]
    fake.embedded_dapr = _tracking("dapr")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "application_sdk.dev", fake)

    _run_to_completion(warm.warm())

    assert events == ["enter temporal", "enter dapr", "exit dapr", "exit temporal"]


@pytest.mark.parametrize(
    "workflow",
    [".github/workflows/warm-caches.yaml", ".github/workflows/sdk-tests-reusable.yaml"],
)
def test_seeder_and_consumer_share_one_cache_definition(workflow: str) -> None:
    """The main-branch seeder and the integration job must restore the same
    key; both go through the one composite action, and neither inlines it."""
    text = (_REPO / workflow).read_text(encoding="utf-8")
    assert _CACHE_ACTION in text
    assert "key: embedded-runtimes-" not in text
