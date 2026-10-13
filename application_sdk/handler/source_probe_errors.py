"""Typed error leaves for the network source probes (ADR-0021).

One leaf per thing a customer does differently to fix it, so a consumer routes
on ``code`` rather than on a message. None of them carries the configured host,
a resolved address or a raw driver message as evidence: an outcome row must not
surface a customer hostname beyond what preflight already shows, and a socket
or certificate error's text routinely embeds both. Evidence is limited to
values the SDK itself chose (an errno name, a verification code, the port).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    InvalidInputError,
    NotFoundError,
    PreconditionError,
    SourceUnavailableError,
)


@dataclass(kw_only=True)
class SourceHostMalformedError(InvalidInputError):
    """The configured host is not a bare host name or address.

    A scheme (``https://``), a path or whitespace in the host field. Caught here
    rather than by the driver, which reports it as a failed login.
    """

    code: ClassVar[str] = "INVALID_INPUT_SOURCE_HOST_MALFORMED"
    message: str = "The configured source host is not a bare host name"


@dataclass(kw_only=True)
class SourceAddressDisallowedError(InvalidInputError):
    """Every address the host resolved to is one the SDK will not connect to.

    Loopback, link-local (which includes the cloud metadata endpoint),
    unspecified, multicast or reserved. A source never legitimately lives
    there; a connection setting that points at one is a mistake or a probe of
    the worker's own network.
    """

    code: ClassVar[str] = "INVALID_INPUT_SOURCE_ADDRESS_DISALLOWED"
    message: str = "The configured source host resolves to a disallowed address"
    address_class: str | None = None


@dataclass(kw_only=True)
class SourceHostNotFoundError(NotFoundError):
    """DNS answered authoritatively that the host name does not exist."""

    code: ClassVar[str] = "NOT_FOUND_SOURCE_HOST"
    message: str = "The configured source host name does not exist"
    resource_type: str | None = "host"


@dataclass(kw_only=True)
class SourceDnsUnavailableError(SourceUnavailableError):
    """DNS did not answer: a resolver timeout or a server failure."""

    code: ClassVar[str] = "SOURCE_UNAVAILABLE_DNS"
    message: str = "The source host name could not be resolved"


@dataclass(kw_only=True)
class SourceConnectRefusedError(SourceUnavailableError):
    """The host answered, and nothing is listening on the port."""

    code: ClassVar[str] = "SOURCE_UNAVAILABLE_TCP_REFUSED"
    message: str = "The source refused the connection"
    port: int | None = None


@dataclass(kw_only=True)
class SourceConnectTimeoutError(SourceUnavailableError):
    """No answer to the connection attempt: typically a firewall dropping it."""

    code: ClassVar[str] = "SOURCE_UNAVAILABLE_TCP_TIMEOUT"
    message: str = "The connection to the source timed out"
    port: int | None = None


@dataclass(kw_only=True)
class SourceUnreachableError(SourceUnavailableError):
    """The network reported no route to the source."""

    code: ClassVar[str] = "SOURCE_UNAVAILABLE_TCP_UNREACHABLE"
    message: str = "There is no network route to the source"
    port: int | None = None


@dataclass(kw_only=True)
class SourceTlsCertificateError(PreconditionError):
    """The source's certificate failed verification.

    An untrusted chain, an expired certificate, or one not issued for the
    configured host. Waiting does not fix it, and the SDK never offers to skip
    verification: the remedy is a valid certificate or a trusted CA.
    """

    code: ClassVar[str] = "PRECONDITION_SOURCE_TLS_CERTIFICATE"
    message: str = "The source's TLS certificate failed verification"
    verify_code: int | None = None


@dataclass(kw_only=True)
class SourceTlsHandshakeError(PreconditionError):
    """The TLS handshake failed for a reason other than the certificate.

    Usually a port that does not speak TLS, or no protocol version both sides
    support.
    """

    code: ClassVar[str] = "PRECONDITION_SOURCE_TLS_HANDSHAKE"
    message: str = "The TLS handshake with the source failed"


@dataclass(kw_only=True)
class SourceTlsTimeoutError(SourceUnavailableError):
    """The source accepted the connection but did not finish the handshake."""

    code: ClassVar[str] = "SOURCE_UNAVAILABLE_TLS_TIMEOUT"
    message: str = "The TLS handshake with the source timed out"
