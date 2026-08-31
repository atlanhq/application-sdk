"""The gate's store write, over a real socket and real httpx.

Lives here rather than in the unit suite for two reasons. The unit legs run with
``--disable-socket`` and under ``-n auto``, where a test that stands a server up
and holds a connection open is both an exception to the guard and sensitive to
what else shares its worker. And what these assert — that the write genuinely
leaves the process and that no failure of it reaches the caller — is a property
of the transport, which a stubbed client cannot show either way: a stub returns
immediately whether the real one would or not.

The ordering itself is pinned deterministically, without a socket, in
``tests/unit/execution/test_preflight_persist.py``. Nothing here needs Temporal
or any running service — only loopback.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from application_sdk.contracts.base import Input
from application_sdk.execution._temporal import preflight_persist as persist
from application_sdk.execution._temporal.preflight_gate import PreflightGateInput
from application_sdk.handler.contracts import PreflightOutput, PreflightStatus

pytestmark = pytest.mark.integration

SLUG = "example-source-aBcD1234"


class _ExtractionInput(Input):
    """A minimal gate-eligible input, shaped like a connector's."""

    extraction_method: str = ""
    credential_guid: str = ""
    agent_json: object | None = None
    connection: dict = {}


def _gate(**overrides) -> PreflightGateInput:
    return PreflightGateInput(entrypoint="extract-metadata", **overrides)


def _verdict(message: str = "all good") -> PreflightOutput:
    return PreflightOutput(status=PreflightStatus.READY, message=message)


class TestTheWriteReallyLeavesTheProcess:
    """No stubs below the HTTP client: a real socket, over real httpx.

    Every other test here substitutes ``post_check_result``, which is the right
    seam for asserting *what* is sent — but it cannot show the send is genuinely
    off the caller's path, because a stub returns immediately whether the real
    thing would or not.

    Each scenario runs under a hard deadline, so a regression that makes the
    write blocking fails the test instead of hanging the suite.
    """

    DEADLINE = 10.0

    @staticmethod
    def _run(scenario) -> None:
        async def _bounded():
            await asyncio.wait_for(
                scenario(), timeout=TestTheWriteReallyLeavesTheProcess.DEADLINE
            )

        asyncio.run(_bounded())

    @staticmethod
    @asynccontextmanager
    async def _serving(handler):
        """A server on a free loopback port, torn down without waiting on handlers.

        Handlers are tracked and cancelled rather than awaited: one of them holds
        a connection open on purpose, and ``wait_closed`` would block on it.
        """
        live: set[asyncio.Task[None]] = set()

        async def _tracked(reader, writer):
            task = asyncio.current_task()
            assert task is not None
            live.add(task)
            try:
                await handler(reader, writer)
            finally:
                live.discard(task)
                writer.close()

        server = await asyncio.start_server(_tracked, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}/continuous-preflight/check-results"
        finally:
            for task in list(live):
                task.cancel()
            server.close()

    @staticmethod
    def _respond(status: bytes, body: bytes = b""):
        async def _handler(reader, writer):
            await reader.read(1)
            writer.write(
                b"HTTP/1.1 " + status + b"\r\nConnection: close\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()

        return _handler

    def _persist(self, endpoint: str, timeout: float = 5.0):
        return persist.persist_check_result(
            _gate(workflow_slug=SLUG), _verdict(), endpoint=endpoint, timeout=timeout
        )

    def test_the_caller_returns_before_the_store_answers(self):
        # The point of the whole design: the app endpoint opens a Polaris catalog,
        # may create the table and commits Parquet. Were this ever awaited, every
        # gated run in the fleet would carry that latency.
        connected = asyncio.Event()

        async def _never_answer(reader, writer):
            connected.set()
            await asyncio.Event().wait()  # held open until cancelled

        async def scenario():
            async with self._serving(_never_answer) as endpoint:
                loop = asyncio.get_running_loop()
                started = loop.time()
                task = self._persist(endpoint, timeout=30)
                assert loop.time() - started < 0.05
                assert task is not None and not task.done()

                # The request does reach the socket, and is still in flight long
                # after the caller moved on.
                await connected.wait()
                assert not task.done()

                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self._run(scenario)

    def test_a_store_that_refuses_the_connection_does_not_reach_the_caller(self):
        async def scenario():
            async with self._serving(self._respond(b"200 OK")) as endpoint:
                pass  # the port is free again, and nothing is listening on it
            task = self._persist(endpoint, timeout=2)
            assert task is not None
            # Awaiting re-raises what the task caught, which no production caller
            # does — the guarantee under test is that scheduling it did not raise.
            await asyncio.gather(task, return_exceptions=True)
            assert isinstance(task.exception(), Exception)

        self._run(scenario)

    def test_a_rejected_row_does_not_reach_the_caller(self):
        async def scenario():
            async with self._serving(self._respond(b"422 Unprocessable Entity")) as e:
                task = self._persist(e)
                assert task is not None
                await task
                assert task.exception() is None

        self._run(scenario)

    def test_a_broken_store_does_not_reach_the_caller(self):
        async def scenario():
            async with self._serving(self._respond(b"503 Service Unavailable")) as e:
                task = self._persist(e)
                assert task is not None
                await task
                assert task.exception() is None

        self._run(scenario)

    def test_a_rejection_never_echoes_the_verdict_into_a_log(self, caplog):
        # A 4xx body is FastAPI's validation error, which renders the input it
        # rejected — and the input is the verdict.
        body = b'{"detail":"invalid payload: db://svc:hunter2@example-host:3306"}'

        async def scenario():
            async with self._serving(
                self._respond(b"422 Unprocessable Entity", body)
            ) as endpoint:
                task = self._persist(endpoint)
                assert task is not None
                await task

        with caplog.at_level("DEBUG"):
            self._run(scenario)
        assert "hunter2" not in caplog.text
        assert "422" in caplog.text  # the status is what does get reported
