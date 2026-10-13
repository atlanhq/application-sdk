"""Network source probes against real sockets on loopback (ADR-0021).

The unit tier fakes the transport; this proves the classification holds for
what the OS and ``ssl`` actually raise. Loopback is refused by the default
address policy, so every test passes a policy that allows it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
import trustme

from application_sdk.handler import source_probes
from application_sdk.handler.source_probes import (
    NetworkProbeTimeouts,
    SourceEndpoint,
    SourceProbeReport,
    TlsMode,
    probe_source_network,
)

pytestmark = pytest.mark.integration

_BOUNDS = NetworkProbeTimeouts(dns_seconds=2, tcp_seconds=2, tls_seconds=2)


def _allow_all(_: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    return None


async def _probe(port: int, tls: TlsMode = TlsMode.DIRECT) -> SourceProbeReport:
    return await probe_source_network(
        [SourceEndpoint(host="localhost", port=port, tls=tls)],
        timeouts=_BOUNDS,
        address_policy=_allow_all,
    )


def _codes(report: SourceProbeReport) -> list[str]:
    return [c.error.code for c in report.failed if c.error is not None]


async def _drain(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        await reader.read(1024)
    finally:
        writer.close()


@pytest_asyncio.fixture
async def serve() -> AsyncIterator[Callable[..., asyncio.Future[int]]]:
    servers: list[asyncio.Server] = []

    async def start(context: ssl.SSLContext | None = None) -> int:
        server = await asyncio.start_server(_drain, "127.0.0.1", 0, ssl=context)
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield start  # type: ignore[misc]
    for server in servers:
        server.close()
        await server.wait_closed()


@pytest.fixture
def ca() -> trustme.CA:
    return trustme.CA()


def _server_context(ca: trustme.CA, *names: str) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(*names).configure_cert(context)
    return context


@pytest.fixture
def trust(monkeypatch: pytest.MonkeyPatch) -> Callable[[trustme.CA], None]:
    def install(ca: trustme.CA) -> None:
        context = ssl.create_default_context()
        ca.configure_trust(context)
        monkeypatch.setattr(source_probes, "get_ssl_context", lambda: context)

    return install


async def _closed_port() -> int:
    server = await asyncio.start_server(_drain, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


async def test_a_closed_port_is_refused():
    report = await _probe(await _closed_port())
    assert _codes(report) == ["SOURCE_UNAVAILABLE_TCP_REFUSED"]


async def test_a_plaintext_port_fails_the_handshake_not_the_certificate(serve):
    report = await _probe(await serve())
    assert _codes(report) == ["PRECONDITION_SOURCE_TLS_HANDSHAKE"]


async def test_an_untrusted_certificate_is_a_certificate_failure(serve, ca):
    report = await _probe(await serve(_server_context(ca, "localhost")))
    assert _codes(report) == ["PRECONDITION_SOURCE_TLS_CERTIFICATE"]


async def test_a_certificate_for_another_host_is_a_certificate_failure(
    serve, ca, trust
):
    trust(ca)
    report = await _probe(await serve(_server_context(ca, "elsewhere.test")))
    assert _codes(report) == ["PRECONDITION_SOURCE_TLS_CERTIFICATE"]


async def test_a_trusted_certificate_passes_all_three(serve, ca, trust):
    trust(ca)
    report = await _probe(await serve(_server_context(ca, "localhost")))
    assert report.failed == [] and len(report.checks) == 3


async def test_a_negotiated_port_is_reached_without_a_handshake(serve):
    report = await _probe(await serve(), tls=TlsMode.NEGOTIATED)
    assert report.failed == [] and len(report.checks) == 2
    assert report.unsupported == ["sourceTls"]


async def test_the_default_policy_refuses_loopback_without_connecting(serve):
    port = await serve()
    report = await probe_source_network(
        [SourceEndpoint(host="localhost", port=port)], timeouts=_BOUNDS
    )
    assert _codes(report) == ["INVALID_INPUT_SOURCE_ADDRESS_DISALLOWED"]
