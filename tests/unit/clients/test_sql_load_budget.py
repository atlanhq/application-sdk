"""``load()`` is bounded and keeps the loop free while it waits.

Building the connection string runs an STS AssumeRole for IAM-role auth, and the
first connect can hang on a black-holed host. Both run off the loop under one
budget (``SQL_CLIENT_CONNECT_TIMEOUT_SECONDS``); expiry is a typed
``SqlClientConnectTimeoutError``. No sockets here: a hang is a thread parked on
an event. The real-socket version is ``tests/integration/clients/test_sql_load_blackhole.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import (
    AsyncBaseSQLClient,
    BaseSQLClient,
    _with_connect_timeout,
)
from application_sdk.clients.sql_errors import SqlClientConnectTimeoutError

_BUDGET = 0.3
#: The FND-4083 bar: the loop never stalls this long while load() waits.
_MAX_LOOP_LAG = 0.1


def _config() -> DatabaseConfig:
    return DatabaseConfig(template="test://{host}", required=["host"])


@contextlib.contextmanager
def _budget(seconds: float) -> Iterator[None]:
    with patch(
        "application_sdk.clients.sql.SQL_CLIENT_CONNECT_TIMEOUT_SECONDS", seconds
    ):
        yield


@contextlib.asynccontextmanager
async def _loop_lag() -> AsyncIterator[list[float]]:
    """Yield a one-item list holding the worst loop stall seen inside the block."""
    worst = [0.0]
    stop = asyncio.Event()

    async def _tick() -> None:
        while not stop.is_set():
            started = time.monotonic()
            await asyncio.sleep(0.01)
            worst[0] = max(worst[0], time.monotonic() - started - 0.01)

    ticker = asyncio.create_task(_tick())
    try:
        yield worst
    finally:
        stop.set()
        await ticker


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """Set at teardown so a parked worker thread always exits."""
    event = threading.Event()
    yield event
    event.set()


class TestWithConnectTimeout:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@db.example.com:5432/d",
            "postgresql+psycopg2://u:p@db.example.com/d",
            "postgresql+psycopg://u:p@db.example.com/d",
            "mysql://u:p@db.example.com/d",
            "mysql+mysqldb://u:p@db.example.com/d",
        ],
    )
    def test_adds_timeout_for_unbounded_drivers(self, url: str) -> None:
        assert _with_connect_timeout(url, {"sslmode": "require"}, 59.2) == {
            "sslmode": "require",
            "connect_timeout": 60,
        }

    @pytest.mark.parametrize(
        "url",
        [
            # Already bounded by default: never loosen.
            "mysql+pymysql://u:p@db.example.com/d",
            "postgresql+asyncpg://u:p@db.example.com/d",
            "snowflake://u:p@account/d",
            # ``timeout`` is a socket read timeout: it would cut long queries.
            "postgresql+pg8000://u:p@db.example.com/d",
            "redshift+redshift_connector://u:p@db.example.com/d",
            "sqlite:///x.db",
            "not a url",
        ],
    )
    def test_leaves_other_drivers_alone(self, url: str) -> None:
        connect_args = {"sslmode": "require"}
        assert _with_connect_timeout(url, connect_args, 60) == connect_args

    def test_keeps_connect_args_value(self) -> None:
        assert _with_connect_timeout(
            "postgresql://u:p@h/d", {"connect_timeout": 300}, 60
        ) == {"connect_timeout": 300}

    def test_keeps_url_query_value(self) -> None:
        # connect_args would override the URL's own setting; leave it be.
        assert (
            _with_connect_timeout("postgresql://u:p@h/d?connect_timeout=300", {}, 60)
            == {}
        )

    def test_does_not_mutate_input(self) -> None:
        connect_args: dict[str, Any] = {}
        _with_connect_timeout("postgresql://u:p@h/d", connect_args, 60)
        assert connect_args == {}


@pytest.mark.asyncio
class TestSyncLoadBudget:
    async def test_hung_connection_string_is_typed_timeout(
        self, release: threading.Event
    ) -> None:
        """A hung STS AssumeRole (inside get_sqlalchemy_connection_string)."""
        client = BaseSQLClient()
        client.DB_CONFIG = _config()

        def _hung_sts() -> str:
            release.wait(10)
            return "test://h"

        client.get_sqlalchemy_connection_string = _hung_sts  # type: ignore[method-assign]
        started = time.monotonic()
        with _budget(_BUDGET), pytest.raises(SqlClientConnectTimeoutError) as exc_info:
            async with _loop_lag() as lag:
                await client.load({})
        assert time.monotonic() - started < _BUDGET + 0.5
        assert lag[0] < _MAX_LOOP_LAG
        assert "within" in exc_info.value.message
        assert client.engine is None

    async def test_hung_ping_is_typed_timeout_and_disposes(
        self, release: threading.Event
    ) -> None:
        client = BaseSQLClient()
        client.DB_CONFIG = _config()
        client.get_sqlalchemy_connection_string = lambda: "test://h"  # type: ignore[method-assign]
        engine = MagicMock()

        def _hung_connect() -> MagicMock:
            release.wait(10)
            return MagicMock()

        engine.connect.side_effect = _hung_connect
        started = time.monotonic()
        with (
            _budget(_BUDGET),
            patch("sqlalchemy.create_engine", return_value=engine),
            pytest.raises(SqlClientConnectTimeoutError),
        ):
            async with _loop_lag() as lag:
                await client.load({})
        assert time.monotonic() - started < _BUDGET + 0.5
        assert lag[0] < _MAX_LOOP_LAG
        engine.dispose.assert_called_once()
        assert client.engine is None

    async def test_driver_timeout_inside_budget_is_not_budget_expiry(self) -> None:
        """A step's own TimeoutError is classified, not reported as the budget."""
        client = BaseSQLClient()
        client.DB_CONFIG = _config()

        def _driver_timeout() -> str:
            raise TimeoutError("timed out")

        client.get_sqlalchemy_connection_string = _driver_timeout  # type: ignore[method-assign]
        with pytest.raises(SqlClientConnectTimeoutError) as exc_info:
            await client.load({})
        assert "within" not in exc_info.value.message
        assert isinstance(exc_info.value.__cause__, TimeoutError)


@pytest.mark.asyncio
class TestAsyncLoadBudget:
    async def test_hung_connection_string_is_typed_timeout(
        self, release: threading.Event
    ) -> None:
        client = AsyncBaseSQLClient()
        client.DB_CONFIG = _config()

        def _hung_sts() -> str:
            release.wait(10)
            return "test://h"

        client.get_sqlalchemy_connection_string = _hung_sts  # type: ignore[method-assign]
        started = time.monotonic()
        with _budget(_BUDGET), pytest.raises(SqlClientConnectTimeoutError):
            async with _loop_lag() as lag:
                await client.load({})
        assert time.monotonic() - started < _BUDGET + 0.5
        assert lag[0] < _MAX_LOOP_LAG

    async def test_hung_ping_is_typed_timeout_and_disposes(self) -> None:
        client = AsyncBaseSQLClient()
        client.DB_CONFIG = _config()
        client.get_sqlalchemy_connection_string = lambda: "test://h"  # type: ignore[method-assign]
        engine = MagicMock()

        @contextlib.asynccontextmanager
        async def _hung_connect() -> AsyncIterator[None]:
            await asyncio.Event().wait()
            yield

        engine.connect.side_effect = _hung_connect
        disposed = asyncio.Event()

        async def _dispose() -> None:
            disposed.set()

        engine.dispose.side_effect = _dispose
        with (
            _budget(_BUDGET),
            patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=engine),
            pytest.raises(SqlClientConnectTimeoutError),
        ):
            await client.load({})
        assert disposed.is_set()
        assert client.engine is None
