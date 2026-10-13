"""Unit tests for the network source probes (ADR-0021).

No sockets: the unit tier runs with ``--disable-socket``. ``socket.getaddrinfo``
and ``asyncio.open_connection`` are replaced at the module boundary the probes
call through, so the probes' own classification and chaining run for real.
Real-socket behaviour (a refused port, a plaintext port, an untrusted
certificate) is in ``tests/integration/test_source_probes.py``.
"""

from __future__ import annotations

import asyncio
import errno
import socket
import ssl
import threading
from collections.abc import Callable
from typing import Any

import orjson
import pytest

from application_sdk.errors.categories import FailureCategory
from application_sdk.handler import source_probes
from application_sdk.handler.source_probes import (
    SOURCE_DNS_CHECK,
    SOURCE_TCP_CHECK,
    SOURCE_TLS_CHECK,
    NetworkProbeTimeouts,
    SourceEndpoint,
    SourceProbeReport,
    TlsMode,
    default_address_policy,
    probe_source_network,
)

_HOST = "warehouse.customer-example.test"
_PUBLIC = "203.0.113.10"  # TEST-NET-3: not reserved by ipaddress, never routed
_FAST = NetworkProbeTimeouts(dns_seconds=0.2, tcp_seconds=0.2, tls_seconds=0.2)


def _resolves_to(*addresses: str) -> Callable[..., list[Any]]:
    def fake(host: str, port: int, **_: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses
        ]

    return fake


def _raises(exc: BaseException) -> Callable[..., Any]:
    def fake(*_: Any, **__: Any) -> Any:
        raise exc

    return fake


class _Writer:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _Connections:
    """Stands in for ``asyncio.open_connection``; records every attempt."""

    def __init__(
        self, plain: BaseException | None = None, tls: BaseException | None = None
    ) -> None:
        self.plain, self.tls = plain, tls
        self.attempts: list[tuple[str, bool]] = []
        self.server_hostnames: list[str | None] = []
        self.contexts: list[ssl.SSLContext] = []

    async def __call__(self, host: str, port: int, **kwargs: Any) -> Any:
        is_tls = "ssl" in kwargs
        self.attempts.append((host, is_tls))
        if is_tls:
            self.contexts.append(kwargs["ssl"])
            self.server_hostnames.append(kwargs.get("server_hostname"))
        error = self.tls if is_tls else self.plain
        if isinstance(error, _Hang):
            await asyncio.sleep(30)
        if error is not None:
            raise error
        return object(), _Writer()


class _Hang(Exception):
    """Marker: the fake connection never completes."""


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> _Connections:
    fake = _Connections()
    monkeypatch.setattr(source_probes.asyncio, "open_connection", fake)
    monkeypatch.setattr(source_probes.socket, "getaddrinfo", _resolves_to(_PUBLIC))
    return fake


async def _probe(host: str = _HOST, **endpoint: Any) -> SourceProbeReport:
    endpoint.setdefault("port", 443)
    return await probe_source_network(
        [SourceEndpoint(host=host, **endpoint)], timeouts=_FAST
    )


def _only_failure(report: SourceProbeReport) -> tuple[str, str, FailureCategory]:
    [failed] = report.failed
    assert failed.error is not None
    return failed.name, failed.error.code, failed.error.category


async def test_reachable_tls_endpoint_passes_all_three(connections: _Connections):
    report = await _probe()
    assert [(c.name, c.passed) for c in report.checks] == [
        (SOURCE_DNS_CHECK, True),
        (SOURCE_TCP_CHECK, True),
        (SOURCE_TLS_CHECK, True),
    ]
    assert report.skipped == report.unsupported == report.broken == []
    assert all(c.duration_ms >= 0 for c in report.checks)
    # Connects to the resolved address, verifies against the configured name.
    assert connections.attempts == [(_PUBLIC, False), (_PUBLIC, True)]
    assert connections.server_hostnames == [_HOST]
    [context] = connections.contexts
    assert context.verify_mode is ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize(
    "host", ["https://" + _HOST, _HOST + "/db", f"{_HOST} ", "my host"]
)
async def test_malformed_host_is_invalid_input_and_never_resolved(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch, host: str
):
    monkeypatch.setattr(
        source_probes.socket, "getaddrinfo", _raises(AssertionError("resolved"))
    )
    report = await _probe(host)
    assert _only_failure(report) == (
        SOURCE_DNS_CHECK,
        "INVALID_INPUT_SOURCE_HOST_MALFORMED",
        FailureCategory.INVALID_INPUT,
    )
    assert report.skipped == [SOURCE_TCP_CHECK, SOURCE_TLS_CHECK]
    assert connections.attempts == []


@pytest.mark.parametrize(
    ("gai_errno", "code", "category", "retryable"),
    [
        (socket.EAI_NONAME, "NOT_FOUND_SOURCE_HOST", FailureCategory.NOT_FOUND, False),
        (
            socket.EAI_AGAIN,
            "SOURCE_UNAVAILABLE_DNS",
            FailureCategory.SOURCE_UNAVAILABLE,
            True,
        ),
    ],
)
async def test_dns_failures_split_nxdomain_from_resolver_outage(
    connections: _Connections,
    monkeypatch: pytest.MonkeyPatch,
    gai_errno: int,
    code: str,
    category: FailureCategory,
    retryable: bool,
):
    monkeypatch.setattr(
        source_probes.socket,
        "getaddrinfo",
        _raises(socket.gaierror(gai_errno, "lookup failed")),
    )
    report = await _probe()
    assert _only_failure(report) == (SOURCE_DNS_CHECK, code, category)
    assert report.failed[0].error is not None
    assert report.failed[0].error.retryable is retryable
    assert report.skipped == [SOURCE_TCP_CHECK, SOURCE_TLS_CHECK]


async def test_an_empty_answer_is_a_resolver_failure_not_a_disallowed_address(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(source_probes.socket, "getaddrinfo", _resolves_to())
    assert _only_failure(await _probe())[1] == "SOURCE_UNAVAILABLE_DNS"


async def test_hung_resolver_is_cut_off_at_the_dns_bound(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    release = threading.Event()

    def hang(*_: Any, **__: Any) -> list[Any]:
        release.wait(5)
        return []

    monkeypatch.setattr(source_probes.socket, "getaddrinfo", hang)
    try:
        report = await asyncio.wait_for(_probe(), timeout=2)
    finally:
        release.set()
    assert _only_failure(report)[1] == "SOURCE_UNAVAILABLE_DNS"
    assert report.failed[0].duration_ms < 1000


@pytest.mark.parametrize(
    ("addresses", "reason"),
    [
        (("127.0.0.1",), "loopback"),
        (("169.254.169.254",), "link_local"),
        (("::ffff:127.0.0.1",), "loopback"),
        (("0.0.0.0",), "unspecified"),
    ],
)
async def test_disallowed_resolution_is_refused_before_any_connect(
    connections: _Connections,
    monkeypatch: pytest.MonkeyPatch,
    addresses: tuple[str, ...],
    reason: str,
):
    monkeypatch.setattr(source_probes.socket, "getaddrinfo", _resolves_to(*addresses))
    report = await _probe()
    assert _only_failure(report)[1] == "INVALID_INPUT_SOURCE_ADDRESS_DISALLOWED"
    error = report.failed[0].error
    assert error is not None and error.evidence["address_class"] == reason
    assert connections.attempts == []


async def test_ip_literal_host_skips_resolution_but_not_the_policy(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        source_probes.socket, "getaddrinfo", _raises(AssertionError("resolved"))
    )
    assert (await _probe(_PUBLIC)).failed == []
    assert _only_failure(await _probe("169.254.169.254"))[1] == (
        "INVALID_INPUT_SOURCE_ADDRESS_DISALLOWED"
    )


async def test_disallowed_addresses_are_dropped_from_a_mixed_answer(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        source_probes.socket, "getaddrinfo", _resolves_to("127.0.0.1", _PUBLIC)
    )
    assert (await _probe()).failed == []
    assert [a for a, _ in connections.attempts] == [_PUBLIC, _PUBLIC]


async def test_private_ranges_are_allowed():
    # PrivateLink and peered VPCs resolve to RFC 1918 space.
    import ipaddress

    assert default_address_policy(ipaddress.ip_address("10.1.2.3")) is None
    assert default_address_policy(ipaddress.ip_address("172.16.0.9")) is None


@pytest.mark.parametrize(
    ("error", "code", "network_error"),
    [
        (
            ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
            "SOURCE_UNAVAILABLE_TCP_REFUSED",
            "ECONNREFUSED",
        ),
        (
            OSError(errno.EHOSTUNREACH, "no route"),
            "SOURCE_UNAVAILABLE_TCP_UNREACHABLE",
            "EHOSTUNREACH",
        ),
        (_Hang(), "SOURCE_UNAVAILABLE_TCP_TIMEOUT", None),
    ],
)
async def test_tcp_failures_are_told_apart(
    connections: _Connections,
    error: BaseException,
    code: str,
    network_error: str | None,
):
    connections.plain = error
    report = await _probe()
    name, got, category = _only_failure(report)
    assert (name, got, category) == (
        SOURCE_TCP_CHECK,
        code,
        FailureCategory.SOURCE_UNAVAILABLE,
    )
    evidence = report.failed[0].error.evidence  # type: ignore[union-attr]
    assert evidence["port"] == 443
    assert evidence.get("network_error") == network_error
    assert report.skipped == [SOURCE_TLS_CHECK]


async def test_tcp_tries_the_next_address_when_one_fails(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    second = "203.0.113.11"
    monkeypatch.setattr(
        source_probes.socket, "getaddrinfo", _resolves_to(_PUBLIC, second)
    )
    real = connections.__call__

    async def first_refuses(host: str, port: int, **kwargs: Any) -> Any:
        if host == _PUBLIC and "ssl" not in kwargs:
            connections.attempts.append((host, False))
            raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        return await real(host, port, **kwargs)

    monkeypatch.setattr(source_probes.asyncio, "open_connection", first_refuses)
    assert (await _probe()).failed == []
    # TLS goes to the address TCP actually reached.
    assert connections.attempts[-1] == (second, True)


def _cert_error() -> ssl.SSLCertVerificationError:
    exc = ssl.SSLCertVerificationError(1, "certificate verify failed")
    exc.verify_code = 10  # X509_V_ERR_CERT_HAS_EXPIRED
    return exc


@pytest.mark.parametrize(
    ("error", "code", "category"),
    [
        (
            _cert_error(),
            "PRECONDITION_SOURCE_TLS_CERTIFICATE",
            FailureCategory.PRECONDITION,
        ),
        (
            ssl.SSLError(1, "wrong version number"),
            "PRECONDITION_SOURCE_TLS_HANDSHAKE",
            FailureCategory.PRECONDITION,
        ),
        (
            ConnectionResetError(errno.ECONNRESET, "reset"),
            "PRECONDITION_SOURCE_TLS_HANDSHAKE",
            FailureCategory.PRECONDITION,
        ),
        (_Hang(), "SOURCE_UNAVAILABLE_TLS_TIMEOUT", FailureCategory.SOURCE_UNAVAILABLE),
    ],
)
async def test_tls_failures_are_told_apart(
    connections: _Connections,
    error: BaseException,
    code: str,
    category: FailureCategory,
):
    connections.tls = error
    report = await _probe()
    assert _only_failure(report) == (SOURCE_TLS_CHECK, code, category)


async def test_certificate_failure_carries_the_verify_code(
    connections: _Connections,
):
    connections.tls = _cert_error()
    report = await _probe()
    assert report.failed[0].error.evidence["verify_code"] == 10  # type: ignore[union-attr]


async def test_a_non_verifying_trust_store_breaks_the_probe_instead_of_passing(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(source_probes, "get_ssl_context", lambda: unverified)
    report = await _probe()
    assert report.broken == [SOURCE_TLS_CHECK]
    assert [c.name for c in report.checks] == [SOURCE_DNS_CHECK, SOURCE_TCP_CHECK]
    assert report.failed == []
    assert connections.contexts == []  # no handshake was attempted at all


@pytest.mark.parametrize(
    ("tls", "unsupported", "names"),
    [
        (TlsMode.NEGOTIATED, [SOURCE_TLS_CHECK], [SOURCE_DNS_CHECK, SOURCE_TCP_CHECK]),
        (TlsMode.NONE, [], [SOURCE_DNS_CHECK, SOURCE_TCP_CHECK]),
    ],
)
async def test_only_direct_tls_is_handshaken(
    connections: _Connections,
    tls: TlsMode,
    unsupported: list[str],
    names: list[str],
):
    report = await _probe(port=5432, tls=tls)
    assert [c.name for c in report.checks] == names
    assert report.unsupported == unsupported
    assert all(not is_tls for _, is_tls in connections.attempts)


async def test_a_raising_probe_fails_open_without_a_row(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        source_probes.socket, "getaddrinfo", _raises(RuntimeError("our bug"))
    )
    report = await _probe()
    assert report.checks == []
    assert report.broken == [SOURCE_DNS_CHECK]
    assert report.skipped == [SOURCE_TCP_CHECK, SOURCE_TLS_CHECK]


async def test_rows_never_carry_the_host_or_an_address(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    # Socket and certificate errors routinely embed both; none may reach a row.
    connections.plain = ConnectionRefusedError(
        errno.ECONNREFUSED, f"Connect call failed ('{_PUBLIC}', 443) to {_HOST}"
    )
    report = await _probe()
    wire = orjson.dumps([c.to_wire() for c in report.checks]).decode()
    assert _HOST not in wire and _PUBLIC not in wire
    assert "customer-example" not in wire


async def test_several_endpoints_are_labelled_and_independent(
    connections: _Connections, monkeypatch: pytest.MonkeyPatch
):
    def resolve(host: str, port: int, **_: Any) -> list[Any]:
        if host == "auth.example.test":
            raise socket.gaierror(socket.EAI_NONAME, "nx")
        return _resolves_to(_PUBLIC)(host, port)

    monkeypatch.setattr(source_probes.socket, "getaddrinfo", resolve)
    report = await probe_source_network(
        [
            SourceEndpoint(host=_HOST, port=443, label="api"),
            SourceEndpoint(host="auth.example.test", port=443, label="oauth"),
        ],
        timeouts=_FAST,
    )
    assert [(c.name, c.passed) for c in report.checks] == [
        ("sourceDns.api", True),
        ("sourceTcp.api", True),
        ("sourceTls.api", True),
        ("sourceDns.oauth", False),
    ]
    assert report.skipped == ["sourceTcp.oauth", "sourceTls.oauth"]


@pytest.mark.parametrize("labels", [(None, None), ("a", "a"), ("a", None)])
async def test_several_endpoints_need_distinct_labels(labels: tuple[str | None, ...]):
    with pytest.raises(ValueError, match="distinct labels"):
        await probe_source_network(
            [SourceEndpoint(host=_HOST, port=443, label=label) for label in labels]
        )


async def test_no_endpoints_is_an_empty_report():
    assert await probe_source_network([]) == SourceProbeReport()
