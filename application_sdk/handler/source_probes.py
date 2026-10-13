"""Network source probes: DNS, then TCP, then TLS, as typed preflight rows (ADR-0021).

Answers "can this worker reach the source at all?" before anything presents a
credential, so a firewall, a typo in the host or an untrusted certificate is
reported as what it is rather than as the failed login a driver's single
``connect()`` turns it into.

Warn-only: :func:`probe_source_network` returns a :class:`SourceProbeReport`
and decides nothing. Nothing in the SDK merges its rows into a verdict yet; the
ADR's graduation path does that only once the outcome events show the rows
agree with real run failures.

Every probe is bounded by its own timeout, never raises, and never offers to
skip certificate verification. Rows carry no host name, resolved address or
raw socket text: see :mod:`application_sdk.handler.source_probe_errors`.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from application_sdk._runtime.offload import run_in_thread
from application_sdk.clients.ssl_utils import get_ssl_context
from application_sdk.contracts.base import SerializableEnum
from application_sdk.errors.base import AppError
from application_sdk.handler.contracts import PreflightCheck
from application_sdk.handler.source_probe_errors import (
    SourceAddressDisallowedError,
    SourceConnectRefusedError,
    SourceConnectTimeoutError,
    SourceDnsUnavailableError,
    SourceHostMalformedError,
    SourceHostNotFoundError,
    SourceTlsCertificateError,
    SourceTlsHandshakeError,
    SourceTlsTimeoutError,
    SourceUnreachableError,
)

#: Canonical check names. One name per question, whatever the app: the fleet
#: had 22 names for "can we reach it", which made it impossible to compare.
SOURCE_DNS_CHECK: Final = "sourceDns"
SOURCE_TCP_CHECK: Final = "sourceTcp"
SOURCE_TLS_CHECK: Final = "sourceTls"

#: At most this many resolved addresses are tried for TCP, inside one deadline.
_MAX_CONNECT_ADDRESSES = 3

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Returns why an address is disallowed (``"loopback"``, …) or ``None``.
AddressPolicy = Callable[[_IpAddress], str | None]


class TlsMode(SerializableEnum):
    """How the source speaks TLS on the declared port.

    Only ``DIRECT`` can be probed generically: the handshake starts as soon as
    the socket opens (HTTPS, Snowflake, Databricks). ``NEGOTIATED`` sources
    upgrade inside their own wire protocol (PostgreSQL ``SSLRequest``, MySQL,
    SQL Server TDS), so a raw handshake against them proves nothing; that check
    belongs to a dialect hook. ``NONE`` declares plaintext.
    """

    DIRECT = "direct"
    NEGOTIATED = "negotiated"
    NONE = "none"


class SourceEndpoint(BaseModel):
    """One network endpoint the app will connect to, as the app declares it."""

    model_config = ConfigDict(frozen=True)

    host: str = Field(..., min_length=1)
    """Host name or IP literal, exactly as configured. Validated by the DNS
    probe rather than here, so a malformed value becomes a typed row instead of
    an exception in the app's endpoint derivation."""

    port: int = Field(..., ge=1, le=65535)

    tls: TlsMode = TlsMode.DIRECT

    label: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    """Suffix for the check names when an app declares several endpoints
    (``sourceTcp.oauth``). Required to be distinct in that case."""


class NetworkProbeTimeouts(BaseModel):
    """Per-probe bounds, in seconds. Each probe is cut off at its own bound."""

    model_config = ConfigDict(frozen=True)

    dns_seconds: float = Field(default=5.0, gt=0, le=30)
    tcp_seconds: float = Field(default=5.0, gt=0, le=30)
    tls_seconds: float = Field(default=10.0, gt=0, le=30)


class SourceProbeReport(BaseModel):
    """What the probes found. Every probe is accounted for in exactly one list.

    A probe whose prerequisite failed is in :attr:`skipped`, never a failed row:
    a "not run" row counted as a failure is how one app's blocks came to be
    attributed to checks that never executed.
    """

    checks: list[PreflightCheck] = []
    """One row per probe that ran, passed or failed."""

    skipped: list[str] = []
    """Probes not run because an earlier probe on the same endpoint failed."""

    unsupported: list[str] = []
    """Probes that do not apply to the endpoint (TLS on a ``NEGOTIATED`` port)."""

    broken: list[str] = []
    """Probes that raised. A broken probe is our defect, not the source's, so it
    produces no row and never counts against the source."""

    @property
    def failed(self) -> list[PreflightCheck]:
        return [c for c in self.checks if not c.passed]


class _ProbeState:
    """What one endpoint's chain has learned so far."""

    def __init__(self) -> None:
        self.addresses: list[_IpAddress] = []
        self.connected: _IpAddress | None = None


def default_address_policy(address: _IpAddress) -> str | None:
    """Refuse addresses a customer source never legitimately lives at.

    Private ranges are allowed: PrivateLink and peered VPCs resolve there.
    Link-local covers the cloud metadata endpoint (169.254.169.254).
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    for name in ("loopback", "link_local", "unspecified", "multicast", "reserved"):
        if getattr(address, f"is_{name}"):
            return name
    return None


def _check_name(base: str, endpoint: SourceEndpoint) -> str:
    return base if endpoint.label is None else f"{base}.{endpoint.label}"


def _elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000.0


def _row(
    base: str, endpoint: SourceEndpoint, started: float, error: AppError | None
) -> PreflightCheck:
    return PreflightCheck(
        name=_check_name(base, endpoint),
        passed=error is None,
        error=error.to_failure_details() if error is not None else None,
        duration_ms=_elapsed_ms(started),
    )


def _malformed(host: str) -> bool:
    return "://" in host or "/" in host or any(ch.isspace() for ch in host)


_NXDOMAIN_ERRNOS = frozenset(
    e
    for e in (
        getattr(socket, "EAI_NONAME", None),
        getattr(socket, "EAI_NODATA", None),
    )
    if e is not None
)


def _dns_error(exc: BaseException) -> AppError:
    if isinstance(exc, socket.gaierror) and exc.errno in _NXDOMAIN_ERRNOS:
        return SourceHostNotFoundError(
            suggested_action=(
                "Check the host name in the connection settings. If the source is "
                "only resolvable through a private DNS zone, make sure that zone "
                "is reachable from Atlan."
            ),
        )
    return SourceDnsUnavailableError(
        network_error=type(exc).__name__,
        suggested_action="DNS did not answer. Retry; if it persists, check the DNS "
        "servers that resolve the source's host name.",
    )


async def _probe_dns(
    endpoint: SourceEndpoint,
    state: _ProbeState,
    timeouts: NetworkProbeTimeouts,
    policy: AddressPolicy,
) -> PreflightCheck:
    started = time.monotonic()
    host = endpoint.host.strip("[]")
    if _malformed(host):
        return _row(
            SOURCE_DNS_CHECK,
            endpoint,
            started,
            SourceHostMalformedError(
                field="host",
                suggested_action="Enter only the host name, without a scheme "
                "(https://), path or spaces.",
            ),
        )
    try:
        addresses: list[_IpAddress] = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            async with asyncio.timeout(timeouts.dns_seconds):
                infos = await run_in_thread(
                    socket.getaddrinfo,
                    host,
                    endpoint.port,
                    type=socket.SOCK_STREAM,
                )
        except (OSError, TimeoutError) as exc:
            return _row(SOURCE_DNS_CHECK, endpoint, started, _dns_error(exc))
        addresses = []
        for info in infos:
            # sockaddr[0] is the address; IPv6 may carry a "%scope" suffix.
            parsed = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            if parsed not in addresses:
                addresses.append(parsed)
        if not addresses:
            return _row(
                SOURCE_DNS_CHECK,
                endpoint,
                started,
                _dns_error(socket.gaierror("empty answer")),
            )
    allowed = [a for a in addresses if policy(a) is None]
    if not allowed:
        reasons = sorted({policy(a) or "" for a in addresses})
        return _row(
            SOURCE_DNS_CHECK,
            endpoint,
            started,
            SourceAddressDisallowedError(
                address_class=",".join(reasons),
                suggested_action="Point the connection at the source's own host "
                "name, not a local or metadata address.",
            ),
        )
    state.addresses = allowed
    return _row(SOURCE_DNS_CHECK, endpoint, started, None)


def _connect_error(exc: BaseException, port: int) -> AppError:
    if isinstance(exc, TimeoutError):
        return SourceConnectTimeoutError(
            port=port,
            suggested_action="Nothing answered on this port. Check that the source "
            "allows connections from Atlan's egress addresses and that the port "
            "is correct.",
        )
    code = getattr(exc, "errno", None)
    if isinstance(exc, ConnectionRefusedError) or code == errno.ECONNREFUSED:
        return SourceConnectRefusedError(
            port=port,
            network_error="ECONNREFUSED",
            suggested_action="The host is reachable but nothing is listening on "
            "this port. Check the port and that the service is running.",
        )
    return SourceUnreachableError(
        port=port,
        network_error=errno.errorcode.get(code, type(exc).__name__)
        if isinstance(code, int)
        else type(exc).__name__,
        suggested_action="There is no network route to the source. Check the "
        "network path (VPN, PrivateLink, peering) between Atlan and the source.",
    )


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except (OSError, TimeoutError, ssl.SSLError):
        pass


async def _probe_tcp(
    endpoint: SourceEndpoint,
    state: _ProbeState,
    timeouts: NetworkProbeTimeouts,
    policy: AddressPolicy,
) -> PreflightCheck:
    started = time.monotonic()
    first_error: BaseException | None = None
    try:
        async with asyncio.timeout(timeouts.tcp_seconds):
            for address in state.addresses[:_MAX_CONNECT_ADDRESSES]:
                try:
                    _, writer = await asyncio.open_connection(
                        str(address), endpoint.port
                    )
                except OSError as exc:
                    first_error = first_error or exc
                    continue
                await _close(writer)
                state.connected = address
                return _row(SOURCE_TCP_CHECK, endpoint, started, None)
    except TimeoutError as exc:
        first_error = first_error or exc
    assert first_error is not None  # the loop ran at least once: DNS passed
    return _row(
        SOURCE_TCP_CHECK, endpoint, started, _connect_error(first_error, endpoint.port)
    )


def _verifying_context() -> ssl.SSLContext:
    """The SDK's trust store, with verification that cannot be turned off here."""
    configured = get_ssl_context()
    context = (
        configured
        if isinstance(configured, ssl.SSLContext)
        else ssl.create_default_context()
    )
    if context.verify_mode is not ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError("source TLS probe refuses a non-verifying SSL context")
    return context


def _tls_error(exc: BaseException) -> AppError:
    if isinstance(exc, TimeoutError):
        return SourceTlsTimeoutError(
            suggested_action="The source accepted the connection but did not "
            "complete a TLS handshake. Check that this port serves TLS.",
        )
    if isinstance(exc, ssl.SSLCertVerificationError):
        return SourceTlsCertificateError(
            verify_code=exc.verify_code,
            suggested_action="Install a certificate valid for this host name and "
            "issued by a trusted CA, or add the issuing CA to Atlan's trust store.",
        )
    return SourceTlsHandshakeError(
        suggested_action="Check that this port serves TLS and supports TLS 1.2 or "
        "later.",
    )


async def _probe_tls(
    endpoint: SourceEndpoint,
    state: _ProbeState,
    timeouts: NetworkProbeTimeouts,
    policy: AddressPolicy,
) -> PreflightCheck:
    started = time.monotonic()
    assert state.connected is not None  # TCP passed
    context = _verifying_context()
    try:
        async with asyncio.timeout(timeouts.tls_seconds):
            _, writer = await asyncio.open_connection(
                str(state.connected),
                endpoint.port,
                ssl=context,
                server_hostname=endpoint.host.strip("[]"),
            )
    except (OSError, TimeoutError) as exc:  # ssl.SSLError is an OSError
        return _row(SOURCE_TLS_CHECK, endpoint, started, _tls_error(exc))
    await _close(writer)
    return _row(SOURCE_TLS_CHECK, endpoint, started, None)


async def _probe_endpoint(
    endpoint: SourceEndpoint,
    timeouts: NetworkProbeTimeouts,
    policy: AddressPolicy,
    report: SourceProbeReport,
) -> None:
    state = _ProbeState()
    chain = [(SOURCE_DNS_CHECK, _probe_dns), (SOURCE_TCP_CHECK, _probe_tcp)]
    if endpoint.tls is TlsMode.DIRECT:
        chain.append((SOURCE_TLS_CHECK, _probe_tls))
    elif endpoint.tls is TlsMode.NEGOTIATED:
        report.unsupported.append(_check_name(SOURCE_TLS_CHECK, endpoint))
    for position, (base, probe) in enumerate(chain):
        try:
            row = await probe(endpoint, state, timeouts, policy)
        except Exception:  # noqa: BLE001 — our defect: fail open, never a row
            report.broken.append(_check_name(base, endpoint))
            row = None
        if row is not None:
            report.checks.append(row)
        if row is None or not row.passed:
            report.skipped.extend(
                _check_name(b, endpoint) for b, _ in chain[position + 1 :]
            )
            return


async def probe_source_network(
    endpoints: Sequence[SourceEndpoint],
    *,
    timeouts: NetworkProbeTimeouts | None = None,
    address_policy: AddressPolicy = default_address_policy,
) -> SourceProbeReport:
    """Probe each endpoint's DNS, then TCP, then (``DIRECT`` only) TLS.

    Endpoints are probed concurrently; within one endpoint each probe runs only
    if the one before it passed. The worst case per endpoint is the sum of
    ``timeouts``, 20s by default. Never raises for anything the network does.

    Args:
        endpoints: What the app will connect to. Several endpoints must each
            carry a distinct ``label``.
        timeouts: Per-probe bounds; defaults to :class:`NetworkProbeTimeouts`.
        address_policy: Which resolved addresses are refused before any
            connection is attempted. Replace it only in tests.

    Returns:
        A report whose rows are ready to append to ``PreflightOutput.checks``.

    Raises:
        ValueError: Several endpoints without distinct labels — a declaration
            defect, so it fails loudly rather than producing ambiguous rows.
    """
    _require_distinct_labels(endpoints)
    bounds = timeouts or NetworkProbeTimeouts()
    reports = [SourceProbeReport() for _ in endpoints]
    await asyncio.gather(
        *(
            _probe_endpoint(endpoint, bounds, address_policy, report)
            for endpoint, report in zip(endpoints, reports, strict=True)
        )
    )
    merged = SourceProbeReport()
    for report in reports:
        merged.checks.extend(report.checks)
        merged.skipped.extend(report.skipped)
        merged.unsupported.extend(report.unsupported)
        merged.broken.extend(report.broken)
    return merged


def _require_distinct_labels(endpoints: Sequence[SourceEndpoint]) -> None:
    if len(endpoints) < 2:
        return
    labels = [e.label for e in endpoints]
    if None in labels or len(set(labels)) != len(labels):
        raise ValueError("several source endpoints need distinct labels")
