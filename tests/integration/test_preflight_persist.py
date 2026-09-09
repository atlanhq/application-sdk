"""The gate's store write, over a real socket and real httpx.

Lives here rather than in the unit suite for two reasons. The unit legs run with
``--disable-socket`` and under ``-n auto``, where a test that stands a server up
and holds a connection open is both an exception to the guard and sensitive to
what else shares its worker. And what these assert — that the write genuinely
leaves the process and that no failure of it reaches the caller — is a property
of the transport: that no failure of the write — a refused connection, a rejected
row, a broken store — reaches the caller, and that a rejection body never lands in
a log. A stubbed client cannot show any of that either way.

What is deliberately *not* here is the ordering claim — that the caller returns
before the write starts. Asserting it over a socket means catching a connection
mid-flight, which proved sensitive to whatever else shares the suite, and it needs
no socket to prove: ``tests/unit/execution/test_preflight_persist.py`` pins it
deterministically on call ordering instead.

Nothing here needs Temporal or any running service — only loopback.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from application_sdk.execution._temporal import preflight_persist as persist
from application_sdk.execution._temporal.preflight_gate import PreflightGateInput
from application_sdk.handler.contracts import PreflightOutput, PreflightStatus

pytestmark = pytest.mark.integration

SLUG = "example-source-aBcD1234"


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

        Handlers are tracked and cancelled rather than awaited, so a client that
        has not finished reading cannot hold teardown open.
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
    async def _read_http_request(reader) -> tuple[bytes, bytes, bytes]:
        """Request line, header block and body, framed by ``Content-Length``."""
        header_block = b""
        while b"\r\n\r\n" not in header_block:
            chunk = await reader.read(1024)
            if not chunk:
                break
            header_block += chunk
        headers, _, remainder = header_block.partition(b"\r\n\r\n")
        request_line, _, header_bytes = headers.partition(b"\r\n")
        content_length = 0
        for line in header_bytes.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.lower() == b"content-length":
                content_length = int(value.strip() or 0)
                break
        body = remainder
        while len(body) < content_length:
            chunk = await reader.read(content_length - len(body))
            if not chunk:
                break
            body += chunk
        return request_line, header_bytes, body[:content_length]

    @staticmethod
    def _respond(status: bytes, body: bytes = b"", captured: list | None = None):
        async def _handler(reader, writer):
            (
                request_line,
                header_bytes,
                request_body,
            ) = await TestTheWriteReallyLeavesTheProcess._read_http_request(reader)
            if captured is not None:
                captured.append((request_line, header_bytes, request_body))
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

    def test_the_posted_request_matches_the_row_contract(self):
        captured: list[tuple[bytes, bytes, bytes]] = []

        async def scenario():
            async with self._serving(
                self._respond(b"201 Created", captured=captured)
            ) as e:
                task = self._persist(e)
                assert task is not None
                await task

        self._run(scenario)
        assert len(captured) == 1
        request_line, header_bytes, body = captured[0]
        method, path, _ = request_line.split(b" ", 2)
        assert method == b"POST"
        assert path == b"/continuous-preflight/check-results"
        headers = {
            name.lower(): value.strip()
            for line in header_bytes.split(b"\r\n")
            if line
            for name, _, value in (line.partition(b":"),)
        }
        assert headers[b"content-type"] == b"application/json"
        posted = persist.PreflightCheckResult.model_validate_json(body)
        expected = persist.build_check_result(_gate(workflow_slug=SLUG), _verdict())
        assert expected is not None
        assert posted == persist.PreflightCheckResult.model_validate_json(
            expected.model_dump_json(exclude_none=True)
        )

    def test_a_client_timeout_does_not_reach_the_caller(self):
        async def _hang(reader, writer):
            await asyncio.sleep(30)

        async def scenario():
            async with self._serving(_hang) as endpoint:
                task = self._persist(endpoint, timeout=0.2)
                assert task is not None
                outcome = await asyncio.gather(task, return_exceptions=True)
                assert isinstance(outcome[0], httpx.TimeoutException)
                assert isinstance(task.exception(), httpx.TimeoutException)

        self._run(scenario)
