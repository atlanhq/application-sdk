"""Oracle login rejections must be actionable authentication failures."""

from __future__ import annotations

import pytest
from sqlalchemy import exc as sa_exc

from application_sdk.clients._sql_failure_classifier import (
    SqlFailureKind,
    classify_sql_failure,
)
from application_sdk.clients.sql import _load_failure
from application_sdk.clients.sql_errors import SqlClientAuthFailedError
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.execution._temporal.preflight_gate import gate_blocks
from application_sdk.handler.contracts import PreflightGateMode

_ORACLE_AUTH_MESSAGES = [
    "ORA-01017: invalid username/password; logon denied",
    "ora-01017",
    "invalid username/password",
    "ORA-28000: the account is locked",
    "ORA-28001: the password has expired",
    "ORA-01005: null password given; logon denied",
]


@pytest.mark.parametrize("message", _ORACLE_AUTH_MESSAGES)
def test_oracle_login_rejection_is_auth(message: str) -> None:
    assert classify_sql_failure("DatabaseError", message) is SqlFailureKind.AUTH


@pytest.mark.parametrize("message", _ORACLE_AUTH_MESSAGES)
def test_wrapped_oracle_login_rejection_blocks_hard_gate(message: str) -> None:
    # SQLAlchemy chains the DBAPI error; no Oracle server or driver is needed.
    driver_error = type("DatabaseError", (Exception,), {})(message)
    try:
        raise sa_exc.DatabaseError(None, None, driver_error) from driver_error
    except sa_exc.DatabaseError as wrapped:
        failure = _load_failure(wrapped)

    assert type(failure) is SqlClientAuthFailedError
    assert failure.category is FailureCategory.AUTH
    assert failure.audience is Audience.USER
    assert failure.effective_retryable is False
    assert failure.__cause__ is not None
    assert failure.__cause__.__cause__ is driver_error
    details = failure.to_failure_details()
    assert details.category is FailureCategory.AUTH
    assert gate_blocks(PreflightGateMode.HARD, details.category) is True
    assert gate_blocks(PreflightGateMode.SOFT, details.category) is False


@pytest.mark.parametrize(
    "message",
    [
        "ORA-00942: table or view does not exist",
        "ORA-12514: listener does not currently know of service requested",
        "ORA-03113: end-of-file on communication channel",
    ],
)
def test_other_oracle_errors_are_not_auth(message: str) -> None:
    assert classify_sql_failure("DatabaseError", message) is SqlFailureKind.UNCLASSIFIED


def test_oracle_login_timeout_keeps_timeout_priority() -> None:
    assert (
        classify_sql_failure("TimeoutError", _ORACLE_AUTH_MESSAGES[0])
        is SqlFailureKind.TIMEOUT
    )
