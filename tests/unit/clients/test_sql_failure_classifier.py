"""Table-driven tests for the SQL connect-failure classifier.

Every SQL connect surface (``BaseSQLClient.load``, ``AsyncBaseSQLClient.load``,
``SqlApp.prime_sql_auth``) takes its verdict from here, so each row is a driver
error shape that surface must classify the same way.
"""

from __future__ import annotations

import socket
import ssl

import pytest

from application_sdk.clients._sql_failure_classifier import (
    SqlFailureKind,
    classify_sql_exception,
    classify_sql_failure,
    root_cause,
)

AUTH = SqlFailureKind.AUTH
TIMEOUT = SqlFailureKind.TIMEOUT
UNAVAILABLE = SqlFailureKind.SOURCE_UNAVAILABLE
UNCLASSIFIED = SqlFailureKind.UNCLASSIFIED


@pytest.mark.parametrize(
    ("error_type", "error_message", "expected"),
    [
        # Timeout: by class name, or by a driver phrasing in the message.
        ("TimeoutError", "", TIMEOUT),
        ("ConnectTimeout", "anything", TIMEOUT),
        ("OperationalError", "connection timed out", TIMEOUT),
        ("OperationalError", "Login timeout expired", TIMEOUT),
        ("Error", "[HYT00] [Microsoft][ODBC Driver 18] Timeout expired", TIMEOUT),
        # Auth: by class name.
        ("AuthenticationError", "", AUTH),
        ("AccessDeniedException", "", AUTH),
        ("SnowflakeAuthFailure", "", AUTH),
        # Auth: an overloaded class (OperationalError) decided by the message.
        ("OperationalError", "(1045, \"Access denied for user 'u'@'h'\")", AUTH),
        ("OperationalError", 'password authentication failed for user "u"', AUTH),
        ("ProgrammingError", "Login failed for user 'u'.", AUTH),
        ("DatabaseError", "Invalid credentials supplied", AUTH),
        ("DatabaseError", "authentication error", AUTH),
        ("DatabaseError", "Authentication failed.", AUTH),
        ("TTransportException", "Authentication failure", AUTH),
        ("TTransportException", "Bad SASL result", AUTH),
        # Network / transport, by class name.
        ("OperationalError", "could not connect to server", UNAVAILABLE),
        ("InterfaceError", "connection already closed", UNAVAILABLE),
        ("ConnectionError", "", UNAVAILABLE),
        ("ConnectionRefusedError", "[Errno 111] Connection refused", UNAVAILABLE),
        ("gaierror", "Name or service not known", UNAVAILABLE),
        ("SSLError", "wrong version number", UNAVAILABLE),
        ("SSLCertVerificationError", "certificate verify failed", UNAVAILABLE),
        ("OSError", "Network is unreachable", UNAVAILABLE),
        ("SourceUnavailableError", "", UNAVAILABLE),
        ("DependencyUnavailableError", "", UNAVAILABLE),
        ("SocketError", "", UNAVAILABLE),
        # Unclassified: nothing matched, left to the connector.
        ("ProgrammingError", "syntax error at or near SELECT", UNCLASSIFIED),
        ("ValueError", "bad port", UNCLASSIFIED),
        ("TTransportException", "TSocket read 0 bytes", UNCLASSIFIED),
        ("TTransportException", "Could not start SASL", UNCLASSIFIED),
        ("", "", UNCLASSIFIED),
    ],
)
def test_classify_sql_failure(
    error_type: str, error_message: str, expected: SqlFailureKind
) -> None:
    assert classify_sql_failure(error_type, error_message) is expected


@pytest.mark.parametrize(
    ("error_type", "error_message", "expected"),
    [
        # Timeout is checked first: a login timeout and a rejected password are
        # both OperationalError, and an auth word in a timeout message must not
        # turn it into AUTH.
        ("AuthenticationError", "timed out", TIMEOUT),
        ("OperationalError", "login failed: timed out", TIMEOUT),
        # Auth before network: the same class with an auth message is AUTH.
        ("ConnectionError", "access denied", AUTH),
        ("OSError", "Login failed", AUTH),
    ],
)
def test_precedence(
    error_type: str, error_message: str, expected: SqlFailureKind
) -> None:
    assert classify_sql_failure(error_type, error_message) is expected


@pytest.mark.parametrize(
    ("error_type", "error_message"),
    [
        ("OPERATIONALERROR", "ACCESS DENIED"),
        ("operationalerror", "access denied"),
    ],
)
def test_case_insensitive(error_type: str, error_message: str) -> None:
    assert classify_sql_failure(error_type, error_message) is AUTH


def _chain(*excs: BaseException) -> BaseException:
    """Link ``excs`` outermost first, each raised ``from`` the next."""
    for outer, inner in zip(excs, excs[1:]):
        outer.__cause__ = inner
    return excs[0]


class OperationalError(Exception):
    """Stands in for a DBAPI ``OperationalError``; only the class name matters."""


class SQLClientWrapperError(Exception):
    """A wrapper whose own name and message say nothing about the cause."""


def test_root_cause_of_a_bare_exception_is_itself() -> None:
    exc = ValueError("x")
    assert root_cause(exc) is exc


def test_root_cause_walks_to_the_innermost() -> None:
    inner = OperationalError("access denied")
    outer = _chain(SQLClientWrapperError("load failed"), RuntimeError("wrap"), inner)
    assert root_cause(outer) is inner


def test_root_cause_ignores_implicit_context() -> None:
    outer = SQLClientWrapperError("load failed")
    outer.__context__ = OperationalError("access denied")
    assert root_cause(outer) is outer


def test_root_cause_stops_on_a_cycle() -> None:
    a = SQLClientWrapperError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert root_cause(a) is b


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # The wrapper says nothing; the innermost driver error decides.
        (
            _chain(
                SQLClientWrapperError("load failed"),
                OperationalError("(1045, 'Access denied for user')"),
            ),
            AUTH,
        ),
        (
            _chain(
                SQLClientWrapperError("load failed"),
                OperationalError("could not connect"),
            ),
            UNAVAILABLE,
        ),
        (_chain(SQLClientWrapperError("x"), socket.timeout("timed out")), TIMEOUT),
        (
            _chain(SQLClientWrapperError("x"), ssl.SSLCertVerificationError("bad")),
            UNAVAILABLE,
        ),
        (_chain(SQLClientWrapperError("x"), socket.gaierror("no host")), UNAVAILABLE),
        # The wrapper's own message is never read once there is a cause.
        (
            _chain(SQLClientWrapperError("access denied"), ValueError("bad port")),
            UNCLASSIFIED,
        ),
        (SQLClientWrapperError("load failed"), UNCLASSIFIED),
    ],
)
def test_classify_sql_exception(exc: BaseException, expected: SqlFailureKind) -> None:
    assert classify_sql_exception(exc) is expected
