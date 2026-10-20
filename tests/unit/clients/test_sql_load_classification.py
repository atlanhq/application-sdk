"""``BaseSQLClient.load`` types a connect failure by what actually failed.

Before FND-3889 every failure — DNS, refused connection, TLS, login timeout,
even a credential parse error — reached the user as ``AUTH_SQL_CLIENT_FAILED``.
The unit tier bans sockets, so each case raises a constructed driver error the
way SQLAlchemy does: a ``sqlalchemy.exc.OperationalError`` raised from the
DBAPI error.
"""

from __future__ import annotations

import ssl
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2
import pytest
from sqlalchemy import exc as sa_exc

from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import AsyncBaseSQLClient, BaseSQLClient
from application_sdk.clients.sql_errors import (
    MissingSqlParamError,
    SqlClientAuthFailedError,
    SqlClientConnectTimeoutError,
    SqlClientConnectUnclassifiedError,
    SqlClientSourceUnavailableError,
    SqlCredentialsParseError,
)
from application_sdk.errors import AppError
from application_sdk.errors.categories import Audience, FailureCategory

_PASSWORD = "hunter2-not-a-real-secret"


def _driver_error(name: str, *args: Any) -> Exception:
    """A DBAPI error whose class name matches a driver we cannot install here."""
    return type(name, (Exception,), {})(*args)


def _pyodbc_login_timeout() -> Exception:
    return _driver_error(
        "OperationalError",
        "HYT00",
        "[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Login timeout expired "
        "(0) (SQLDriverConnect)",
    )


def _mysql_access_denied() -> Exception:
    return _driver_error(
        "OperationalError",
        1045,
        "Access denied for user 'svc'@'10.0.0.1' (using password: YES)",
    )


def _thrift_sasl(message: str) -> Exception:
    """impyla raises Thrift's own transport error; SQLAlchemy never rewraps it."""
    return _driver_error("TTransportException", message)


def _wrapped(orig: Exception) -> sa_exc.OperationalError:
    """Raise ``orig`` the way SQLAlchemy does, so ``__cause__`` is set."""
    try:
        raise sa_exc.OperationalError(
            f"postgresql://svc:{_PASSWORD}@db.example.invalid/x", None, orig
        ) from orig
    except sa_exc.OperationalError as e:
        return e


# (driver error factory, expected leaf, expected category)
_CASES: list[tuple[str, Any, type[AppError], FailureCategory]] = [
    (
        "psycopg2-dns",
        lambda: psycopg2.OperationalError(
            'could not translate host name "db.example.invalid" to address: '
            "Name or service not known"
        ),
        SqlClientSourceUnavailableError,
        FailureCategory.SOURCE_UNAVAILABLE,
    ),
    (
        "psycopg2-refused",
        lambda: psycopg2.OperationalError(
            'connection to server at "db.example.invalid" (10.0.0.5), port 5432 '
            "failed: Connection refused"
        ),
        SqlClientSourceUnavailableError,
        FailureCategory.SOURCE_UNAVAILABLE,
    ),
    (
        "psycopg2-tls",
        lambda: psycopg2.OperationalError(
            "SSL error: certificate verify failed: self-signed certificate"
        ),
        SqlClientSourceUnavailableError,
        FailureCategory.SOURCE_UNAVAILABLE,
    ),
    (
        "psycopg2-password",
        lambda: psycopg2.OperationalError(
            'FATAL:  password authentication failed for user "svc"'
        ),
        SqlClientAuthFailedError,
        FailureCategory.AUTH,
    ),
    (
        "psycopg2-connect-timeout",
        lambda: psycopg2.OperationalError(
            'connection to server at "db.example.invalid" failed: timeout expired'
        ),
        SqlClientConnectTimeoutError,
        FailureCategory.TIMEOUT,
    ),
    (
        "pyodbc-hyt00",
        _pyodbc_login_timeout,
        SqlClientConnectTimeoutError,
        FailureCategory.TIMEOUT,
    ),
    (
        "mysql-1045",
        _mysql_access_denied,
        SqlClientAuthFailedError,
        FailureCategory.AUTH,
    ),
    (
        "thrift-bad-sasl-result",
        lambda: _thrift_sasl("Bad SASL result: authentication failure"),
        SqlClientAuthFailedError,
        FailureCategory.AUTH,
    ),
    (
        "ssl-cert-verification",
        lambda: ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
        ),
        SqlClientSourceUnavailableError,
        FailureCategory.SOURCE_UNAVAILABLE,
    ),
    (
        "thrift-sasl-no-local-mechanism",
        lambda: _thrift_sasl(
            "Could not start SASL: Error in sasl_client_start (-4) "
            "SASL(-4): no mechanism available"
        ),
        SqlClientConnectUnclassifiedError,
        FailureCategory.INTERNAL,
    ),
    (
        "thrift-tsocket-eof",
        lambda: _thrift_sasl("TSocket read 0 bytes"),
        SqlClientConnectUnclassifiedError,
        FailureCategory.INTERNAL,
    ),
    (
        "unrecognised",
        lambda: RuntimeError("driver hiccup"),
        SqlClientConnectUnclassifiedError,
        FailureCategory.INTERNAL,
    ),
]


def _config() -> DatabaseConfig:
    return DatabaseConfig(
        template="test://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        connect_args={},
    )


@pytest.fixture
def sync_client() -> BaseSQLClient:
    client = BaseSQLClient()
    client.DB_CONFIG = _config()
    client.get_sqlalchemy_connection_string = lambda: "test_connection_string"  # type: ignore[method-assign]
    return client


@pytest.fixture
def async_client() -> AsyncBaseSQLClient:
    client = AsyncBaseSQLClient()
    client.DB_CONFIG = _config()
    client.get_sqlalchemy_connection_string = lambda: "test_connection_string"  # type: ignore[method-assign]
    return client


async def _load_sync_failing_with(client: BaseSQLClient, error: Exception) -> None:
    with (
        patch("sqlalchemy.create_engine", return_value=MagicMock()),
        patch(
            "application_sdk.clients.sql.run_in_thread",
            AsyncMock(side_effect=error),
        ),
    ):
        await client.load({"username": "svc", "password": _PASSWORD})


async def _load_async_failing_with(
    client: AsyncBaseSQLClient, error: Exception
) -> None:
    class _FailingConnect:
        async def __aenter__(self) -> None:
            raise error

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    engine = AsyncMock()
    engine.connect = MagicMock(return_value=_FailingConnect())
    with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine):
        await client.load({"username": "svc", "password": _PASSWORD})


def _assert_typed(
    raised: AppError, leaf: type[AppError], category: FailureCategory
) -> None:
    assert type(raised) is leaf
    assert raised.category is category
    # The driver error rides ``cause``; it never reaches the message, which
    # would carry the connection string (password included) onto the wire.
    assert _PASSWORD not in raised.message
    assert _PASSWORD not in (raised.to_failure_details().cause_repr or "")


@pytest.mark.parametrize(
    ("make_error", "leaf", "category"),
    [case[1:] for case in _CASES],
    ids=[case[0] for case in _CASES],
)
class TestLoadClassifiesDriverFailures:
    async def test_sync(
        self,
        sync_client: BaseSQLClient,
        make_error: Any,
        leaf: type[AppError],
        category: FailureCategory,
    ) -> None:
        with pytest.raises(AppError) as exc_info:
            await _load_sync_failing_with(sync_client, _wrapped(make_error()))
        _assert_typed(exc_info.value, leaf, category)
        assert sync_client.engine is None

    async def test_async(
        self,
        async_client: AsyncBaseSQLClient,
        make_error: Any,
        leaf: type[AppError],
        category: FailureCategory,
    ) -> None:
        with pytest.raises(AppError) as exc_info:
            await _load_async_failing_with(async_client, _wrapped(make_error()))
        _assert_typed(exc_info.value, leaf, category)
        assert async_client.engine is None


def test_unreachable_source_routes_to_user_and_is_retryable() -> None:
    """The hard gate retries SOURCE_UNAVAILABLE; it never retries AUTH."""
    err = SqlClientSourceUnavailableError()
    assert err.audience is Audience.USER
    assert err.effective_retryable is True


def test_connect_timeout_routes_to_user() -> None:
    assert SqlClientConnectTimeoutError.audience is Audience.USER


def test_unclassified_is_flagged_pending() -> None:
    err = SqlClientConnectUnclassifiedError()
    assert err.classification_pending is True
    assert err.category is FailureCategory.INTERNAL


class TestLoadKeepsTypedErrors:
    """An ``AppError`` already says what failed; ``load`` must not relabel it."""

    async def test_missing_param_from_connection_string(
        self, sync_client: BaseSQLClient
    ) -> None:
        def _missing() -> str:
            raise MissingSqlParamError(field="host")

        sync_client.get_sqlalchemy_connection_string = _missing  # type: ignore[method-assign]
        with pytest.raises(MissingSqlParamError) as exc_info:
            await sync_client.load({"username": "svc"})
        assert exc_info.value.category is FailureCategory.INVALID_INPUT
        assert exc_info.value.__cause__ is None

    async def test_credentials_parse_error_from_connector(
        self, async_client: AsyncBaseSQLClient
    ) -> None:
        def _unparseable() -> str:
            raise SqlCredentialsParseError(field="compiled_url")

        async_client.get_sqlalchemy_connection_string = _unparseable  # type: ignore[method-assign]
        with pytest.raises(SqlCredentialsParseError) as exc_info:
            await async_client.load({"username": "svc"})
        assert exc_info.value.category is FailureCategory.INVALID_INPUT

    async def test_typed_error_from_ping_disposes_engine(
        self, sync_client: BaseSQLClient
    ) -> None:
        engine = MagicMock()
        typed = SqlCredentialsParseError(field="extra")
        with (
            patch("sqlalchemy.create_engine", return_value=engine),
            patch(
                "application_sdk.clients.sql.run_in_thread",
                AsyncMock(side_effect=["test_connection_string", typed]),
            ),
            pytest.raises(SqlCredentialsParseError) as exc_info,
        ):
            await sync_client.load({"username": "svc"})
        assert exc_info.value is typed
        engine.dispose.assert_called_once()
        assert sync_client.engine is None
