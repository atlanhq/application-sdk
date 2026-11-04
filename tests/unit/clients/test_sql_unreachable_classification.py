"""SQL connection failures with explicit unreachable-source driver messages."""

from __future__ import annotations

import pytest
from sqlalchemy import exc as sa_exc

from application_sdk.clients._sql_failure_classifier import (
    SqlFailureKind,
    classify_sql_failure,
)
from application_sdk.clients.sql import _load_failure
from application_sdk.clients.sql_errors import SqlClientSourceUnavailableError
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.execution._temporal.preflight_gate import gate_blocks
from application_sdk.handler.contracts import PreflightGateMode
from application_sdk.templates.sql_app import SqlApp

_MESSAGES = [
    pytest.param(
        "ORA-12170: Cannot connect. TCP connect timeout of 20s for host "
        "10.0.0.1 port 1521.",
        id="oracle-tcp-timeout",
    ),
    pytest.param("ORA-12170: TNS:Connect timeout occurred", id="ora-12170"),
    pytest.param("DPY-6005: cannot connect to database", id="dpy-6005"),
    pytest.param("TCP connect timeout: connection timed out", id="tcp-timeout"),
]


class DatabaseError(Exception):
    """Oracle DBAPI error shape, without installing or connecting a driver."""


@pytest.mark.parametrize("message", _MESSAGES)
def test_unreachable_message_is_source_unavailable(message: str) -> None:
    """Explicit unreachable-source evidence takes precedence over timeout text."""
    assert (
        classify_sql_failure("DatabaseError", message)
        is SqlFailureKind.SOURCE_UNAVAILABLE
    )
    assert (
        classify_sql_failure("TimeoutError", message)
        is SqlFailureKind.SOURCE_UNAVAILABLE
    )


@pytest.mark.parametrize("message", _MESSAGES)
def test_unreachable_load_failure_routes_to_source_owner(message: str) -> None:
    """SQLAlchemy wrapping must preserve the source classification and cause."""
    root = DatabaseError(message)
    try:
        raise sa_exc.DatabaseError(None, None, root) from root
    except sa_exc.DatabaseError as wrapped:
        failure = _load_failure(wrapped)
        assert type(failure) is SqlClientSourceUnavailableError
        assert failure.category is FailureCategory.SOURCE_UNAVAILABLE
        assert failure.audience is Audience.USER
        assert failure.effective_retryable is True
        assert failure.__cause__ is wrapped
        assert message not in failure.message


@pytest.mark.parametrize("message", _MESSAGES)
def test_unreachable_load_failure_blocks_hard_gate(message: str) -> None:
    """The classified failure blocks hard mode, not the advisory soft gate."""
    failure = _load_failure(DatabaseError(message))
    assert gate_blocks(PreflightGateMode.HARD, failure.category) is True
    assert gate_blocks(PreflightGateMode.SOFT, failure.category) is False


@pytest.mark.parametrize("message", _MESSAGES)
def test_unreachable_prime_probe_routes_to_source_owner(message: str) -> None:
    """The auth-prime surface shares the SQL connect classifier."""
    failure = SqlApp._classify_probe_strings("DatabaseError", message)
    assert failure.category is FailureCategory.SOURCE_UNAVAILABLE
    assert failure.audience is Audience.USER


@pytest.mark.parametrize(
    ("error_type", "message", "expected"),
    [
        ("DatabaseError", "ORA-01017: invalid credentials", SqlFailureKind.AUTH),
        ("TimeoutError", "operation timed out", SqlFailureKind.TIMEOUT),
        ("OperationalError", "HYT00: Login timeout expired", SqlFailureKind.TIMEOUT),
        ("DatabaseError", "unknown driver failure", SqlFailureKind.UNCLASSIFIED),
    ],
)
def test_other_driver_failures_keep_their_classification(
    error_type: str, message: str, expected: SqlFailureKind
) -> None:
    """Do not turn generic clock overruns or rejected credentials into outages."""
    assert classify_sql_failure(error_type, message) is expected
