"""``load()`` is bounded and keeps the loop free while it waits.

Building the connection string runs an STS AssumeRole for IAM-role auth, and the
first connect can hang on a black-holed host. Both run off the loop under one
budget (``SQL_CLIENT_CONNECT_TIMEOUT_SECONDS``); expiry is a typed
``SqlClientConnectTimeoutError``. No sockets here: a hang is a thread parked on
an event. The real-socket version is ``tests/integration/clients/test_sql_load_blackhole.py``.

No assertion here reads a wall-clock delta. "The loop stayed free" is a
:class:`~tests.unit.loop_probe.LoopProbe` callback; "load() gave up at the
budget" is the hung thread still being parked when ``load()`` raised; the
budget arithmetic runs on a stepped loop clock.
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
    _cap_next_connect_timeout,
    _with_connect_timeout,
)
from application_sdk.clients.sql_errors import SqlClientConnectTimeoutError
from tests.unit.loop_probe import LoopProbe

_BUDGET = 0.3
#: How long a hung step stays parked unless released. Far past ``_BUDGET``: a
#: load() that waited for the step instead of giving up would see it return.
_HANG_SECONDS = 30.0


def _config() -> DatabaseConfig:
    return DatabaseConfig(template="test://{host}", required=["host"])


@contextlib.contextmanager
def _budget(seconds: float) -> Iterator[None]:
    with patch(
        "application_sdk.clients.sql.SQL_CLIENT_CONNECT_TIMEOUT_SECONDS", seconds
    ):
        yield


class _Hang:
    """A blocking step that parks its worker thread until released.

    ``block`` pays the probe's toll first, so the probe sees whether the loop
    ran while the step was in progress. ``returned`` is set only when the park
    ends, so a ``load()`` that raised while it is unset gave up on its own
    budget rather than waiting for the step. ``recorded`` is set once the probe
    has its outcome.
    """

    def __init__(self, probe: LoopProbe) -> None:
        self.probe = probe
        self.release = threading.Event()
        self.returned = threading.Event()
        self.recorded = threading.Event()

    def block(self) -> None:
        self.probe.block()
        self.recorded.set()
        self.release.wait(_HANG_SECONDS)
        self.returned.set()


@pytest.fixture
def hang() -> Iterator[_Hang]:
    """Released at teardown so a parked worker thread always exits."""
    step = _Hang(LoopProbe())
    yield step
    step.release.set()


async def _assert_gave_up_on_the_budget(step: _Hang) -> None:
    """Call with ``step.probe`` still bound.

    On a loaded runner ``load()`` can give up before the worker thread reaches
    the probe, or before the probe has recorded. Waiting here, with the probe
    bound and the loop free, lets a late worker record instead of finding an
    unbound probe or an empty ``outcomes``.
    """
    assert not step.returned.is_set(), (
        "load() raised only after the hung step returned: it waited for the "
        "worker thread instead of giving up at its own budget"
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + step.probe.stall_seconds * 2
    while not step.recorded.is_set() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    step.probe.assert_loop_stayed_free("load()")


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


class _Stop(Exception):
    """Raised by the recorder so no connect ever reaches a socket."""


def _record_connect_params(engine: Any) -> list[dict[str, Any]]:
    """Record each connect's driver params, then stop it before any socket opens.

    Registered after the cap, so it sees the params the driver would get.
    """
    from sqlalchemy import event

    seen: list[dict[str, Any]] = []

    def _record(
        dialect: object, conn_rec: object, cargs: object, cparams: dict[str, Any]
    ) -> None:
        seen.append(dict(cparams))
        raise _Stop

    event.listen(engine, "do_connect", _record)
    return seen


class TestCapNextConnectTimeout:
    @pytest.mark.parametrize(
        ("connect_args", "url_query", "first"),
        [
            ({"connect_timeout": 60}, "", 7),
            # Already below the cap: kept.
            ({"connect_timeout": 3}, "", 3),
            # A URL value arrives as a string; capped all the same.
            ({}, "?connect_timeout=300", 7),
        ],
    )
    def test_caps_only_the_next_connect(
        self, connect_args: dict[str, Any], url_query: str, first: int
    ) -> None:
        pytest.importorskip("psycopg2")
        from sqlalchemy import create_engine

        url = f"postgresql+psycopg2://u:p@127.0.0.1:1/d{url_query}"
        engine = create_engine(url, connect_args=connect_args)
        _cap_next_connect_timeout(engine, url, 6.2)
        seen = _record_connect_params(engine)
        for _ in range(2):
            with pytest.raises(_Stop):
                engine.raw_connection()
        assert int(seen[0]["connect_timeout"]) == first
        # Later connects keep the configured value.
        assert (
            seen[1]["connect_timeout"]
            == (connect_args or {"connect_timeout": "300"})["connect_timeout"]
        )

    def test_leaves_other_drivers_and_doubles_alone(self) -> None:
        engine = MagicMock()
        _cap_next_connect_timeout(engine, "postgresql://u:p@h/d", 5)
        _cap_next_connect_timeout(engine, "mysql+pymysql://u:p@h/d", 5)
        engine.assert_not_called()


class TestSyncLoadBudget:
    @pytest.mark.parametrize("client_cls", [BaseSQLClient, AsyncBaseSQLClient])
    async def test_sts_sees_the_load_deadline(
        self, client_cls: type[BaseSQLClient]
    ) -> None:
        """The STS call inside get_sqlalchemy_connection_string sees load()'s deadline.

        The worker thread is not stopped when load() gives up, so the call
        must be told when to stop on its own.
        """
        from application_sdk.common import aws_utils

        client = client_cls()
        client.DB_CONFIG = _config()
        seen: list[float | None] = []

        def _build() -> str:
            seen.append(aws_utils._STS_DEADLINE.get())
            raise _Stop

        client.get_sqlalchemy_connection_string = _build  # type: ignore[method-assign]
        started = time.monotonic()
        with _budget(30.0), pytest.raises(Exception):
            await client.load({})
        ended = time.monotonic()
        assert len(seen) == 1 and seen[0] is not None
        # The deadline is the budget measured from when load() began, which
        # falls between the two readings. Bracketed, not a delta: no slack.
        assert started + 30.0 <= seen[0] <= ended + 30.0
        # Scoped to load(): nothing leaks into the caller's context.
        assert aws_utils._STS_DEADLINE.get() is None

    async def test_ping_gets_only_the_budget_left_after_sts(self) -> None:
        """A slow STS leaves less time for the ping; its driver timeout shrinks to match.

        Otherwise a black-holed connect would run on in its worker thread for the
        full timeout after load() had already given up.
        """
        client = BaseSQLClient()
        client.DB_CONFIG = _config()
        # The budget is measured on the loop's clock. Stepped by hand here, it
        # reads exactly what STS cost, however long the runner took.
        loop = asyncio.get_running_loop()
        now = [loop.time()]

        def _slow_sts() -> str:
            now[0] += 1.2
            return "postgresql+psycopg2://u:p@127.0.0.1:1/d"

        client.get_sqlalchemy_connection_string = _slow_sts  # type: ignore[method-assign]
        caps: list[float] = []
        engine = MagicMock()
        with (
            _budget(3.0),
            patch.object(loop, "time", lambda: now[0]),
            patch("sqlalchemy.create_engine", return_value=engine),
            patch(
                "application_sdk.clients.sql._cap_next_connect_timeout",
                side_effect=lambda _e, _cs, seconds: caps.append(seconds),
            ),
        ):
            await client.load({})
        assert caps == [pytest.approx(3.0 - 1.2)]

    async def test_hung_connection_string_is_typed_timeout(self, hang: _Hang) -> None:
        """A hung STS AssumeRole (inside get_sqlalchemy_connection_string)."""
        client = BaseSQLClient()
        client.DB_CONFIG = _config()

        def _hung_sts() -> str:
            hang.block()
            return "test://h"

        client.get_sqlalchemy_connection_string = _hung_sts  # type: ignore[method-assign]
        with hang.probe.bound():
            with (
                _budget(_BUDGET),
                pytest.raises(SqlClientConnectTimeoutError) as exc_info,
            ):
                await client.load({})
            await _assert_gave_up_on_the_budget(hang)
        assert "within" in exc_info.value.message
        assert client.engine is None

    async def test_hung_ping_is_typed_timeout_and_disposes(self, hang: _Hang) -> None:
        client = BaseSQLClient()
        client.DB_CONFIG = _config()
        client.get_sqlalchemy_connection_string = lambda: "test://h"  # type: ignore[method-assign]
        engine = MagicMock()

        def _hung_connect() -> MagicMock:
            hang.block()
            return MagicMock()

        engine.connect.side_effect = _hung_connect
        with hang.probe.bound():
            with (
                _budget(_BUDGET),
                patch("sqlalchemy.create_engine", return_value=engine),
                pytest.raises(SqlClientConnectTimeoutError),
            ):
                await client.load({})
            await _assert_gave_up_on_the_budget(hang)
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


class TestAsyncLoadBudget:
    async def test_hung_connection_string_is_typed_timeout(self, hang: _Hang) -> None:
        client = AsyncBaseSQLClient()
        client.DB_CONFIG = _config()

        def _hung_sts() -> str:
            hang.block()
            return "test://h"

        client.get_sqlalchemy_connection_string = _hung_sts  # type: ignore[method-assign]
        with hang.probe.bound():
            with _budget(_BUDGET), pytest.raises(SqlClientConnectTimeoutError):
                await client.load({})
            await _assert_gave_up_on_the_budget(hang)

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
