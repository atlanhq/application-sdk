"""Classify a failed SQL connect by what actually went wrong.

One reader for every SQL connect surface: ``BaseSQLClient.load`` /
``AsyncBaseSQLClient.load`` and ``SqlApp.prime_sql_auth`` all ask the same
question of a driver exception — was it the credentials, the clock, or the
network path — and answer it here. Each caller builds its own typed leaf from
the verdict, so the wording can differ per surface while the decision cannot.
"""

from __future__ import annotations

import enum


class SqlFailureKind(enum.Enum):
    """What a SQL connect failure was, as far as the driver error can tell."""

    AUTH = "auth"
    TIMEOUT = "timeout"
    SOURCE_UNAVAILABLE = "source_unavailable"
    UNCLASSIFIED = "unclassified"


def root_cause(exc: BaseException) -> BaseException:
    """Walk ``__cause__`` to the innermost exception.

    The SDK's SQL client rewraps driver exceptions, and SQLAlchemy rewraps the
    DBAPI error, so the outermost exception describes a wrapper and the
    innermost describes what actually happened. Cycle-guarded — a hand-built
    ``__cause__`` loop would otherwise hang the worker.
    """
    seen = {id(exc)}
    while exc.__cause__ is not None and id(exc.__cause__) not in seen:
        exc = exc.__cause__
        seen.add(id(exc))
    return exc


# ODBC renders SQLSTATE HYT00 as "Login timeout expired" — no "timeout" in the
# class name and no "timed out" in the message, so a generic substring check
# misses it. Match the driver phrasings explicitly.
_TIMEOUT_MSG_HINTS = ("timed out", "login timeout", "timeout expired", "hyt00")

# Driver error_type for MySQL ``Access denied`` is ``OperationalError`` — that
# class is overloaded across both auth and network failures, so message content
# has to be the tiebreaker.
_AUTH_CLASS_HINTS = ("authentication", "accessdenied", "authfail")
_AUTH_MSG_HINTS = (
    "access denied",
    "1045",
    "authentication failed",
    "authentication error",
    "login failed",
    "password authentication failed",
    "invalid credentials",
    # Oracle rejects logins with a generic DatabaseError class.
    "ora-01017",
    "invalid username/password",
    "ora-28000",
    "ora-28001",
    "ora-01005",
    # Thrift SASL handshake (impyla, PyHive): the server refused the sign-in.
    # Its transport error class says nothing about auth, so only the message can.
    # "TSocket read 0 bytes" and "Bad status: 3" are left out: impalad sends
    # them on a rejected login, but other Thrift servers send them for a dropped
    # connection or a mechanism mismatch, so they are the connector's call.
    # "Could not start SASL" is left out too: the client raises it before any
    # credential reaches the server, e.g. when no local SASL mechanism exists.
    "authentication failure",
    "bad sasl result",
)

# Network / DNS / TLS / connection-refused / generic OperationalError without
# an auth-keyword message.
_NETWORK_CLASS_HINTS = (
    "connectionerror",
    "connectionrefused",
    "gaierror",
    "sslerror",
    # ``ssl.SSLCertVerificationError`` — its name does not contain "sslerror".
    "sslcertverificationerror",
    "oserror",
    "operationalerror",
    "interfaceerror",
    # Match wrapped SDK error classnames surfaced as error_type.
    # "dependencyunavailable" is retained for legacy wrappers that predate the
    # SourceUnavailableError migration.
    "sourceunavailable",
    "dependencyunavailable",
    "socketerror",
)


def classify_sql_failure(error_type: str, error_message: str) -> SqlFailureKind:
    """Map a driver exception's class name and message to a failure kind.

    Checked in order — timeout, then auth, then network — because the classes
    overlap: a login timeout is an ``OperationalError`` and so is a rejected
    password, and the message is what tells them apart.
    """
    type_lower = error_type.lower()
    msg_lower = error_message.lower()

    if "timeout" in type_lower or any(h in msg_lower for h in _TIMEOUT_MSG_HINTS):
        return SqlFailureKind.TIMEOUT
    if any(h in type_lower for h in _AUTH_CLASS_HINTS) or any(
        h in msg_lower for h in _AUTH_MSG_HINTS
    ):
        return SqlFailureKind.AUTH
    if any(h in type_lower for h in _NETWORK_CLASS_HINTS):
        return SqlFailureKind.SOURCE_UNAVAILABLE
    return SqlFailureKind.UNCLASSIFIED


def classify_sql_exception(exc: BaseException) -> SqlFailureKind:
    """Classify a live exception by its root cause's class name and message."""
    root = root_cause(exc)
    return classify_sql_failure(type(root).__name__, str(root))
