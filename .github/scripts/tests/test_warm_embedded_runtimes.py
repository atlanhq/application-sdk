"""Tests for .github/scripts/warm_embedded_runtimes.py and the cache it seeds."""

from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment

from application_sdk.dev import _embedded

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
    monkeypatch.setattr(warm, "time_skipping_server", _tracking("test-server"))

    _run_to_completion(warm.warm())

    assert events == [
        "enter temporal",
        "enter dapr",
        "enter test-server",
        "exit test-server",
        "exit dapr",
        "exit temporal",
    ]


def test_time_skipping_server_downloads_into_the_cached_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test server must land in the dir the cache action saves, not the
    system temp dir a fresh runner starts empty."""
    calls: list[dict[str, object]] = []

    class _Env:
        async def __aenter__(self) -> _Env:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    async def _start(**kwargs: object) -> _Env:
        calls.append(kwargs)
        return _Env()

    monkeypatch.setattr(WorkflowEnvironment, "start_time_skipping", _start)
    monkeypatch.setattr(_embedded, "_download_dir", lambda: "/cached/temporal")

    async def _boot() -> None:
        async with warm.time_skipping_server():
            pass

    _run_to_completion(_boot())

    assert calls == [{"download_dest_dir": "/cached/temporal"}]


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
