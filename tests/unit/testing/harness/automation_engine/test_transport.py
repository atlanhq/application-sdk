"""The AE reader's HTTP pool, and the sync boundary above it.

``AEWorkflowClient``'s behaviour is covered end to end in
``tests/unit/testing/e2e/test_client.py``; what is only reachable here is what
child F changed underneath it — a pooled ``httpx.AsyncClient`` in place of a
single-use one per call, and a typed refusal in place of ``asyncio.run``'s bare
``RuntimeError`` when the sync facade is entered from inside a loop.

The pool has one property worth pinning in both directions. Reusing a
connection is the point on the happy path; reusing one that is already
half-dead is exactly the condition a transport retry exists to escape, which is
why the pool is dropped after a transport error rather than retried into.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from application_sdk.testing.e2e.client import AEWorkflowClient
from application_sdk.testing.harness._errors import SyncBridgeInAsyncContextError
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.automation_engine._errors import (
    AtlanApiTimeoutError,
)
from application_sdk.testing.harness.automation_engine.client import AEClient


class _CountingClient(httpx.AsyncClient):
    """A real ``AsyncClient`` that records how many of itself were built."""

    built: list[_CountingClient] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        type(self).built.append(self)


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> type[_CountingClient]:
    _CountingClient.built = []
    monkeypatch.setattr(httpx, "AsyncClient", _CountingClient)
    return _CountingClient


async def _ok(_self: Any, *_args: Any, **_kwargs: Any) -> httpx.Response:
    return httpx.Response(status_code=200, content=b'{"ok": true}')


class TestPooling:
    async def test_one_pool_serves_every_call(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The happy path pays one connection setup for a whole run, where the
        single-use client paid one per call."""
        monkeypatch.setattr(httpx.AsyncClient, "request", _ok)
        client = AEClient("https://tenant.example.com", "tok")
        for _ in range(5):
            await client._request("GET", "/native-status")
        assert len(counted.built) == 1
        await client.aclose()

    async def test_a_transport_error_drops_the_pool_before_retrying(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retry into the same half-dead connection is not a retry."""
        calls = {"n": 0}

        async def flaky(_self: Any, *_args: Any, **_kwargs: Any) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadError("connection reset")
            return httpx.Response(status_code=200, content=b'{"ok": true}')

        monkeypatch.setattr(httpx.AsyncClient, "request", flaky)
        client = AEClient("https://tenant.example.com", "tok")
        with fake_clock():
            status, _ = await client._request("GET", "/native-status")
        assert status == 200
        assert len(counted.built) == 2
        assert counted.built[0].is_closed
        await client.aclose()

    async def test_aclose_is_idempotent_and_a_later_call_reopens(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(httpx.AsyncClient, "request", _ok)
        client = AEClient("https://tenant.example.com", "tok")
        await client._request("GET", "/native-status")
        await client.aclose()
        await client.aclose()
        await client._request("GET", "/native-status")
        assert len(counted.built) == 2

    async def test_a_sustained_transport_error_still_raises_a_typed_leaf(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pool's lifecycle must not swallow the failure the poll loop's
        transient tolerance keys on."""

        async def dead(_self: Any, *_args: Any, **_kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("name resolution failed")

        monkeypatch.setattr(httpx.AsyncClient, "request", dead)
        client = AEClient("https://tenant.example.com", "tok")
        with fake_clock(), pytest.raises(AtlanApiTimeoutError):
            await client._request("GET", "/native-status")
        await client.aclose()


class TestSyncBoundary:
    def test_the_facade_closes_its_reader(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(httpx.AsyncClient, "request", _ok)
        client = AEWorkflowClient("https://tenant.example.com", "tok")
        client.get_native_status("run-1")
        assert len(counted.built) == 1
        client.close()
        assert counted.built[0].is_closed

    async def test_calling_the_sync_facade_from_a_loop_names_the_async_twin(
        self,
    ) -> None:
        """The gap the five ``asyncio.run`` bridges left open: called from
        inside a running loop they raised a bare ``RuntimeError`` from deep in
        asyncio, with nothing pointing at the fix."""
        client = AEWorkflowClient("https://tenant.example.com", "tok")
        with pytest.raises(SyncBridgeInAsyncContextError) as caught:
            client.get_native_status("run-1")
        assert "_async twin" in str(caught.value)

    async def test_no_asyncio_run_survives_under_testing(self) -> None:
        """FND-242's own done-when, asserted rather than reviewed by eye.

        A reintroduced ``asyncio.run`` is a fresh event loop per call, and on
        the Atlas poll that was ~50 of them — plus ~50 TLS handshakes — for one
        boolean.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[4] / "application_sdk" / "testing"
        offenders = [
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "asyncio.run(" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []
