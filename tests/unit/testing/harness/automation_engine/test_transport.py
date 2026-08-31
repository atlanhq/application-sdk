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

A third property arrived with FND-225: the pool is bound to the event loop that
built it, so a second loop gets a second pool — and the *first* one has to be
released rather than merely dropped on the floor, which is a leak per loop on
any suite that gives each test its own.
"""

from __future__ import annotations

import asyncio
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

        The scan root is found by walking up to ``pyproject.toml`` rather than
        by counting ``parents[N]``. A count is coupled to this file's depth, and
        this file sits one level deeper than its neighbours: ``parents[4]``
        resolved to ``tests/``, making the root ``tests/application_sdk/testing``,
        which does not exist. ``rglob`` over a missing directory yields nothing,
        so the guard passed by scanning zero files.

        Hence the file count below. "Found no offenders" and "looked at nothing"
        are the same empty list, and only one of them is a pass — the same
        distinction :class:`~application_sdk.testing.harness.outcome.Indeterminate`
        exists for, applied to the test that guards it.
        """
        from pathlib import Path

        here = Path(__file__).resolve()
        repo_root = next(
            parent for parent in here.parents if (parent / "pyproject.toml").is_file()
        )
        root = repo_root / "application_sdk" / "testing"
        scanned = sorted(root.rglob("*.py"))
        assert len(scanned) > 50, (
            f"scanned only {len(scanned)} file(s) under {root} — the guard is "
            "looking in the wrong place, not finding a clean tree"
        )
        offenders = [
            path.relative_to(root).as_posix()
            for path in scanned
            if "asyncio.run(" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []


class TestLoopHandoff:
    """A pool belongs to one event loop, and the outgoing one must be released.

    ``httpx`` registers each connection with the loop that opened it, so a pool
    reused from a second loop fails inside ``httpx`` rather than at the seam.
    Rebuilding is therefore right — but rebuilding *without releasing* swapped
    one bug for another: the first pool's sockets stay open with nothing
    referencing them.
    """

    def test_a_second_loop_gets_its_own_pool_and_the_first_is_closed(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two loops, both kept alive, so the release path is actually exercised.

        ``asyncio.run`` twice would close the first loop before the second
        began, which takes the *other* branch — the one where there is nothing
        left to release. Driving two live loops by hand is what makes the close
        observable.
        """
        monkeypatch.setattr(httpx.AsyncClient, "request", _ok)
        client = AEClient("https://tenant.example.com", "tok")

        first_loop = asyncio.new_event_loop()
        second_loop = asyncio.new_event_loop()
        try:
            first_loop.run_until_complete(client._request("GET", "/x"))
            assert len(counted.built) == 1
            first_pool = counted.built[0]
            assert not first_pool.is_closed

            second_loop.run_until_complete(client._request("GET", "/x"))
            assert len(counted.built) == 2, "the second loop must not reuse the pool"

            # The close is scheduled *on the owning loop*, because that is where
            # the connections live. Turning that loop once runs it.
            first_loop.run_until_complete(asyncio.sleep(0))
            assert first_pool.is_closed, (
                "the outgoing pool was dropped without being closed — one "
                "leaked pool per event loop"
            )
        finally:
            first_loop.close()
            second_loop.close()

    def test_a_dead_previous_loop_is_not_an_error(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The common shape, and it must stay silent.

        A closed loop already tore its transports down, so there is nothing to
        release and nothing an operator could act on. What must not happen is a
        raise on the way to serving a perfectly good request.
        """
        monkeypatch.setattr(httpx.AsyncClient, "request", _ok)
        client = AEClient("https://tenant.example.com", "tok")

        asyncio.run(client._request("GET", "/x"))
        status, _ = asyncio.run(client._request("GET", "/x"))

        assert status == 200
        assert len(counted.built) == 2


class TestAsyncContextManager:
    """``async with`` is the shape a caller whose lifetime fits a block wants.

    It is the reason the class has one: teardown expressed as a block is a
    property of the code, where teardown expressed as a separate ``aclose`` is a
    property of the caller remembering — and the second produced a never-closed
    leak twice on ``PortForward`` before it grew the same pair.
    """

    async def test_the_block_closes_the_pool_on_success(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(httpx.AsyncClient, "request", _ok)
        async with AEClient("https://tenant.example.com", "tok") as client:
            await client._request("GET", "/x")
            assert not counted.built[0].is_closed
        assert counted.built[0].is_closed

    async def test_the_block_closes_the_pool_when_the_body_raises(
        self, counted: type[_CountingClient], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The half that matters: a leak on the failure path is the one nobody
        notices, because the failure is what gets read."""
        monkeypatch.setattr(httpx.AsyncClient, "request", _ok)
        with pytest.raises(RuntimeError, match="boom"):
            async with AEClient("https://tenant.example.com", "tok") as client:
                await client._request("GET", "/x")
                raise RuntimeError("boom")
        assert counted.built[0].is_closed

    async def test_entering_opens_nothing(self, counted: type[_CountingClient]) -> None:
        """The pool is still built on first use, so a block that makes no call
        pays for no connection."""
        async with AEClient("https://tenant.example.com", "tok"):
            pass
        assert counted.built == []
