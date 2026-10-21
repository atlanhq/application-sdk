"""``load()`` against black-holed endpoints: a typed error, and the thread let go.

A black hole is a local TCP listener that accepts and then never sends a byte —
the shape of a filtered private link or a hung STS endpoint. Two cases:

* **STS black-holed** (IAM-role auth): the AssumeRole inside
  ``get_sqlalchemy_connection_string`` hangs, and the budget turns the hang into
  ``SqlClientConnectTimeoutError``. Its timeouts are fitted to the budget, so it
  hangs up by then too.
* **DB host black-holed** (psycopg2): the connect hangs in the handshake. The
  driver's ``connect_timeout``, injected by the SDK, ends the attempt and closes
  the socket — the worker thread is freed, not pinned until the OS gives up.

Real sockets, so this is the integration tier.

That the loop stays free while ``load()`` waits is proven in the unit tier
(``tests/unit/clients/test_sql_load_budget.py``) with a ``LoopProbe``, which
needs no threshold. Here a real socket is in the way, so a loop-stall bar
could only measure the runner: this test's first run in a session absorbed
the cold botocore and psycopg2 imports and stalled the loop past one
(FND-4233). Every bound below sits between the budget and the failure it
guards against, far from both.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.clients.sql_errors import SqlClientConnectTimeoutError

pytestmark = pytest.mark.integration

#: libpq rounds a connect_timeout below 2 s up to 2 s.
_BUDGET = 2.0
#: How long load() and its worker thread get to give up. Well past the budget,
#: and well short of what a regression costs: botocore's fixed 15 s STS read
#: timeout, or a psycopg2 connect with no timeout that never ends at all.
_GIVE_UP_WITHIN = 10.0


class _BlackHole:
    """Accepts TCP connections and never answers; records when a peer hangs up."""

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self.accepted = threading.Event()
        self.peer_closed = threading.Event()
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        with contextlib.suppress(OSError):
            while True:
                conn, _ = self._listener.accept()
                self._conns.append(conn)
                self.accepted.set()
                threading.Thread(target=self._drain, args=(conn,), daemon=True).start()

    def _drain(self, conn: socket.socket) -> None:
        # Read and discard whatever the client sends; b"" means it hung up.
        with contextlib.suppress(OSError):
            while conn.recv(4096):
                pass
        self.peer_closed.set()

    def close(self) -> None:
        self._listener.close()
        for conn in self._conns:
            with contextlib.suppress(OSError):
                conn.close()


@pytest.fixture
def black_hole() -> Iterator[_BlackHole]:
    hole = _BlackHole()
    yield hole
    hole.close()


class _PostgresClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="postgresql+psycopg2://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
    )


async def test_black_holed_db_host_times_out_and_frees_the_thread(
    black_hole: _BlackHole,
) -> None:
    pytest.importorskip("psycopg2")
    client = _PostgresClient()
    credentials = {
        "username": "example_user",
        "password": "example_password",
        "host": "127.0.0.1",
        "port": black_hole.port,
        "extra": {"database": "example_db"},
    }

    with (
        patch(
            "application_sdk.clients.sql.SQL_CLIENT_CONNECT_TIMEOUT_SECONDS", _BUDGET
        ),
        pytest.raises(SqlClientConnectTimeoutError),
    ):
        await asyncio.wait_for(client.load(credentials), _GIVE_UP_WITHIN)

    assert black_hole.accepted.is_set(), "the client never reached the black hole"
    # The driver's own connect_timeout gave up and closed the socket: the
    # sdk-blocking- thread is free, not parked until the OS TCP timeout.
    assert await asyncio.to_thread(black_hole.peer_closed.wait, _GIVE_UP_WITHIN)
    assert client.engine is None


async def test_black_holed_sts_times_out_without_blocking_the_loop(
    black_hole: _BlackHole, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Point STS at the black hole and keep boto3 away from any real credential
    # source on the machine: synthetic keys, no config files, no IMDS.
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", f"http://127.0.0.1:{black_hole.port}")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "example-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret-key")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    # Warm boto3 first. A process's first boto3 client loads botocore's JSON
    # models, which on a cold CI runner spends a share of the 2 s budget before
    # AssumeRole is even sent, and the test needs the call to reach the black
    # hole. Built with no network call, against the default session load()
    # also uses.
    boto3 = pytest.importorskip("boto3")
    # A fresh default session, restored at teardown: it caches the synthetic
    # credentials above, which must not leak into later tests in this process.
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    boto3.client("sts", region_name="us-east-1")

    client = _PostgresClient()
    credentials = {
        "authType": "iam_role",
        "username": "example_user",
        "host": "example-db.abc123xyz.us-east-1.rds.amazonaws.com",
        "port": 5432,
        "region": "us-east-1",
        "extra": {
            "aws_role_arn": "arn:aws:iam::123456789012:role/example-role",
            "database": "example_db",
        },
    }

    with (
        patch(
            "application_sdk.clients.sql.SQL_CLIENT_CONNECT_TIMEOUT_SECONDS", _BUDGET
        ),
        pytest.raises(SqlClientConnectTimeoutError),
    ):
        await asyncio.wait_for(client.load(credentials), _GIVE_UP_WITHIN)

    assert black_hole.accepted.is_set(), "AssumeRole never reached the black hole"
    # The STS call's timeouts were fitted to the budget, so it gave up and
    # hung up too: its sdk-blocking- thread is free, not held for botocore's
    # fixed bounds after load() returned.
    assert await asyncio.to_thread(black_hole.peer_closed.wait, _GIVE_UP_WITHIN)
    assert client.engine is None
