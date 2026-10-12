"""run_query binds values instead of splicing them into the SQL text.

Every user-supplied filter in a SQL connector used to be interpolated into the
query, because run_query took only finished SQL and escaped every colon. These
cases pin the bound path: a value reaches the driver as a value however hostile
its text, the query's own colons stay literal, a parameter the query never
references stops the run instead of silently dropping that filter, and a call
without params behaves exactly as before.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from application_sdk.clients.sql import (
    AsyncBaseSQLClient,
    BaseSQLClient,
    _bound_params,
    _escape_colons_for_text,
)
from application_sdk.clients.sql_errors import SqlBindParameterError


class _NamedColumn:
    def __init__(self, column: tuple) -> None:
        self.name = column[0]


class _Result:
    """sqlite3 reports cursor.description rows as plain tuples; run_query reads
    `.name`, as psycopg's and oracledb's column objects provide. Only that is
    adapted -- the statement and its bound values go through real SQLAlchemy
    and SQLite."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.cursor = MagicMock(
            description=[_NamedColumn(c) for c in result.cursor.description]
        )

    def fetchmany(self, size: int) -> list:
        return self._result.fetchmany(size)


class _Connection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, *args: Any) -> _Result:
        return _Result(self._connection.execute(*args))

    def execution_options(self, **options: Any) -> "_Connection":
        self._connection = self._connection.execution_options(**options)
        return self

    def close(self) -> None:
        self._connection.close()


class _SqliteEngine:
    """run_query connects and executes on SDK pool threads, which need not be the
    same thread; sqlite3 refuses cross-thread use unless told otherwise."""

    def __init__(self) -> None:
        self._engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    def connect(self) -> _Connection:
        return _Connection(self._engine.connect())


@pytest.fixture
def sqlite_client() -> BaseSQLClient:
    client = BaseSQLClient()
    client.engine = _SqliteEngine()  # type: ignore[assignment]
    client.use_server_side_cursor = False
    return client


async def _rows(client: BaseSQLClient, query: str, **kwargs: Any) -> list[dict]:
    out: list[dict] = []
    async for batch in client.run_query(query, **kwargs):
        out.extend(batch)
    return out


@pytest.mark.asyncio
async def test_a_bound_value_reaches_the_driver_as_a_value(
    sqlite_client: BaseSQLClient,
):
    hostile = "x' OR '1'='1"
    rows = await _rows(
        sqlite_client, "SELECT :pattern AS v", params={"pattern": hostile}
    )
    assert rows == [{"v": hostile}]


@pytest.mark.asyncio
async def test_the_querys_own_colons_stay_literal_beside_a_bind(
    sqlite_client: BaseSQLClient,
):
    rows = await _rows(sqlite_client, "SELECT :v AS v, '12:30' AS t", params={"v": "a"})
    assert rows == [{"v": "a", "t": "12:30"}]


@pytest.mark.asyncio
async def test_without_params_the_query_runs_as_before(sqlite_client: BaseSQLClient):
    rows = await _rows(sqlite_client, "SELECT '12:30' AS t")
    assert rows == [{"t": "12:30"}]


@pytest.mark.asyncio
async def test_a_param_the_query_never_references_stops_the_run(
    sqlite_client: BaseSQLClient,
):
    """Running without the placeholder would run the query unfiltered."""
    with pytest.raises(SqlBindParameterError):
        await _rows(sqlite_client, "SELECT 1 AS v", params={"pattern": "a"})


@pytest.mark.asyncio
async def test_a_param_name_must_be_an_identifier(sqlite_client: BaseSQLClient):
    with pytest.raises(SqlBindParameterError):
        await _rows(sqlite_client, "SELECT :v AS v", params={"v; DROP": "a"})


def test_a_cast_is_not_mistaken_for_a_bind_of_the_same_name():
    escaped = _escape_colons_for_text(
        "x::v = :v AND t = '12:30'", bind_names=frozenset({"v"})
    )
    assert escaped == "x\\:\\:v = :v AND t = '12\\:30'"


@pytest.mark.asyncio
async def test_a_bind_followed_by_a_cast_binds_and_keeps_the_cast(
    sqlite_client: BaseSQLClient,
):
    """`:name::type` is how Postgres casts a bind; the bind must not be rejected."""
    rows = await _rows(sqlite_client, "SELECT :v AS v, 'a::b' AS t", params={"v": "x"})
    assert rows == [{"v": "x", "t": "a::b"}]
    assert (
        _escape_colons_for_text(
            "c > :since::timestamp", bind_names=frozenset({"since"})
        )
        == "c > :since\\:\\:timestamp"
    )


@pytest.mark.parametrize(
    "query",
    [
        "c > :since::timestamp",
        "x::v = :v AND t = '12:30'",
        "a:v AND :v AND \\:v AND (?:v)",
        "SELECT :v, :since, :other",
    ],
)
def test_validation_and_escaping_agree_with_sqlalchemy_text(query: str):
    """Exactly the names text() binds after escaping are accepted as params."""
    names = frozenset({"v", "since"})
    bound = set(text(_escape_colons_for_text(query, names)).compile().params)
    for name in names:
        if name in bound:
            assert _bound_params(query, {name: 1}) == {name: 1}
        else:
            with pytest.raises(SqlBindParameterError):
                _bound_params(query, {name: 1})


def test_without_bind_names_every_colon_is_escaped():
    assert _escape_colons_for_text("a :v '1:2'") == "a \\:v '1\\:2'"


@pytest.mark.asyncio
async def test_the_async_client_passes_params_to_the_driver():
    client = AsyncBaseSQLClient()
    connection = MagicMock()
    result = MagicMock()
    result.keys.return_value = ["v"]
    result.cursor.fetchmany.side_effect = [[("a",)], []]
    connection.execute = AsyncMock(return_value=result)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=False)
    client.engine = MagicMock()
    client.engine.connect.return_value = context
    client.use_server_side_cursor = False

    with patch("sqlalchemy.text", side_effect=lambda q: q) as text:
        rows = [
            row
            async for batch in client.run_query("SELECT :v AS v", params={"v": "a"})
            for row in batch
        ]

    text.assert_called_once_with("SELECT :v AS v")
    assert connection.execute.await_args.args[1] == {"v": "a"}
    assert rows == [{"v": "a"}]
