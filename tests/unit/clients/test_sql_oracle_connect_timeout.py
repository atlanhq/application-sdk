"""An Oracle TCP connect timeout is classified as a connect timeout.

In a production RCA, python-oracledb reported an unreachable listener as
``ORA-12170: Cannot connect. TCP connect timeout of 20s ...`` on a plain
``DatabaseError``. Neither the class name nor the message matched any hint, so
the failure came back UNCLASSIFIED and ``load()`` raised the INTERNAL
``SqlClientConnectUnclassifiedError``. A hard preflight gate does not block on
INTERNAL, so the run went on to fail later with an "unclassified" error
instead of stopping at preflight with "source unreachable".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import exc as sa_exc

from application_sdk.clients._sql_failure_classifier import (
    SqlFailureKind,
    classify_sql_failure,
)
from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.clients.sql_errors import SqlClientConnectTimeoutError
from application_sdk.errors import AppError
from application_sdk.errors.categories import FailureCategory

_ORA_12170 = (
    "ORA-12170: Cannot connect. TCP connect timeout of 20s for host 10.0.0.1 "
    "port 1521. (CONNECTION_ID=abc)\n"
    "Help: https://docs.oracle.com/error-help/db/ora-12170/"
)


def _oracle_database_error(message: str) -> Exception:
    """``oracledb.DatabaseError`` by class name; the driver is not installed here."""
    return type("DatabaseError", (Exception,), {})(message)


@pytest.mark.parametrize(
    "message",
    [
        _ORA_12170,
        # The error code alone, as some driver versions render it.
        "ORA-12170: TNS:Connect timeout occurred",
    ],
)
def test_oracle_connect_timeout_classifies_as_timeout(message: str) -> None:
    assert classify_sql_failure("DatabaseError", message) is SqlFailureKind.TIMEOUT


async def test_load_raises_connect_timeout_for_ora_12170() -> None:
    client = BaseSQLClient()
    client.DB_CONFIG = DatabaseConfig(
        template="test://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        connect_args={},
    )

    orig = _oracle_database_error(_ORA_12170)
    try:
        raise sa_exc.DatabaseError("oracle+oracledb://svc@h/x", None, orig) from orig
    except sa_exc.DatabaseError as e:
        wrapped: Any = e

    with (
        patch.object(
            client,
            "get_sqlalchemy_connection_string",
            return_value="test_connection_string",
        ),
        patch("sqlalchemy.create_engine", return_value=MagicMock()),
        patch(
            "application_sdk.clients.sql.run_in_thread",
            AsyncMock(side_effect=wrapped),
        ),
        pytest.raises(AppError) as exc_info,
    ):
        await client.load({"username": "svc", "password": "not-a-secret"})

    assert type(exc_info.value) is SqlClientConnectTimeoutError
    assert exc_info.value.category is FailureCategory.SOURCE_UNAVAILABLE
