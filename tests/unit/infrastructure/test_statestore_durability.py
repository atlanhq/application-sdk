"""Durability guard for the SDR statestore (DISTR-849, gap #11).

The SDR e2e component set historically shipped only eventstore / objectstore /
secretstore — no ``statestore`` — and the dev embedded statestore is
``state.in-memory`` (non-durable: a connector's ``save_state`` is lost across a
worker restart / KEDA scale-to-zero). The SDR e2e set now ships a durable
``state.sqlite`` statestore component so a connector that uses
``save_state`` / ``load_state`` is exercised in SDR CI against a durable backend.

This module pins the durability CONTRACT that component provides, driving the
production ``AppContext.save_state`` / ``load_state`` → ``DaprStateStore`` code
path: a value written through the state store survives a FRESH store instance
rebound to the same on-disk backend (the simulated restart / scale-to-zero) —
the property ``state.in-memory`` lacks (asserted by the contrast test).

A SQLite file stands in for Dapr's ``state.sqlite`` backend (the same durable
medium the shipped component uses) so the test is creds-free and needs no live
daprd; the live component itself is exercised end-to-end by the SDR e2e wiring.
The static test at the bottom guards the shipped component against regressing
back to ``state.in-memory`` or being dropped.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from application_sdk.app.context import AppContext
from application_sdk.infrastructure._dapr.client import DaprStateStore

# Repo root: tests/unit/infrastructure/<this file> → parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SDR_STATESTORE_COMPONENT = (
    _REPO_ROOT / ".github/actions/sdr-e2e/components/statestore.yaml"
)


class _SqliteDaprClient:
    """Minimal ``AsyncDaprClient`` stand-in whose state API persists to a SQLite
    file — mirroring Dapr's ``state.sqlite`` durable backend. Only the three
    methods :class:`DaprStateStore` calls are implemented. Every instance rebinds
    to the same file, so a fresh instance sees state written by a prior one — the
    durability the shipped SDR statestore component provides."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS state "
                "(store TEXT, key TEXT, value TEXT, PRIMARY KEY (store, key))"
            )

    async def save_state(self, store_name: str, key: str, value: Any) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO state (store, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(store, key) DO UPDATE SET value = excluded.value",
                (store_name, key, json.dumps(value)),
            )

    async def get_state(self, store_name: str, key: str) -> bytes | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT value FROM state WHERE store = ? AND key = ?",
                (store_name, key),
            ).fetchone()
        return row[0].encode() if row else None

    async def delete_state(self, store_name: str, key: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "DELETE FROM state WHERE store = ? AND key = ?", (store_name, key)
            )


class _InMemoryDaprClient:
    """Non-durable ``AsyncDaprClient`` stand-in — mirrors ``state.in-memory``:
    state lives only in this instance and is gone once it is discarded."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    async def save_state(self, store_name: str, key: str, value: Any) -> None:
        self._data[(store_name, key)] = json.dumps(value)

    async def get_state(self, store_name: str, key: str) -> bytes | None:
        raw = self._data.get((store_name, key))
        return raw.encode() if raw is not None else None

    async def delete_state(self, store_name: str, key: str) -> None:
        self._data.pop((store_name, key), None)


def _ctx(state_store: DaprStateStore) -> AppContext:
    """An AppContext wired to *state_store*, with a fixed app/run so the
    namespaced key (``app:run:key``) is stable across instances."""
    return AppContext(
        app_name="miner-app",
        app_version="1",
        run_id="run-1",
        _state_store=state_store,
    )


class TestStateStoreDurability:
    async def test_save_state_load_state_round_trips(self, tmp_path) -> None:
        """AppContext.save_state → load_state round-trips through the production
        DaprStateStore against a durable (sqlite) backend."""
        client = _SqliteDaprClient(str(tmp_path / "statestore.db"))
        ctx = _ctx(DaprStateStore(client, store_name="statestore"))

        await ctx.save_state("cursor", {"page": 7, "seen": ["a", "b"]})
        assert await ctx.load_state("cursor") == {"page": 7, "seen": ["a", "b"]}

    async def test_state_survives_a_fresh_store_instance(self, tmp_path) -> None:
        """The durability property: state written by one store instance is still
        readable by a FRESH DaprStateStore + client bound to the same on-disk
        backend — i.e. it survives a worker restart / KEDA scale-to-zero. This is
        exactly what ``state.in-memory`` cannot do (see the contrast test)."""
        db_path = str(tmp_path / "statestore.db")

        # Instance #1 writes, then goes away (simulated restart).
        writer = _ctx(
            DaprStateStore(_SqliteDaprClient(db_path), store_name="statestore")
        )
        await writer.save_state("cursor", {"page": 42})

        # Instance #2 is brand new but bound to the same durable backend.
        reader = _ctx(
            DaprStateStore(_SqliteDaprClient(db_path), store_name="statestore")
        )
        assert await reader.load_state("cursor") == {"page": 42}

    async def test_in_memory_backend_loses_state_across_restart(self) -> None:
        """Contrast: with a non-durable (in-memory) backend, a fresh store
        instance has NONE of the prior instance's state — the failure mode the
        durable ``state.sqlite`` SDR component is there to prevent."""
        writer = _ctx(DaprStateStore(_InMemoryDaprClient(), store_name="statestore"))
        await writer.save_state("cursor", {"page": 42})

        reader = _ctx(DaprStateStore(_InMemoryDaprClient(), store_name="statestore"))
        assert await reader.load_state("cursor") is None


class TestSdrStatestoreComponent:
    def test_component_shipped_and_durable(self) -> None:
        """The SDR e2e component set ships a ``statestore`` component and it is a
        DURABLE backend (``state.sqlite``), not ``state.in-memory`` — guards
        against dropping it or regressing it back to non-durable."""
        assert (
            _SDR_STATESTORE_COMPONENT.is_file()
        ), f"SDR statestore component missing at {_SDR_STATESTORE_COMPONENT}"
        assert "name: statestore" in _SDR_STATESTORE_COMPONENT.read_text()
        # Match the component's ``type:`` spec value, not a bare substring — the
        # explanatory comment mentions ``state.in-memory`` on purpose.
        types = {
            line.split(":", 1)[1].strip()
            for line in _SDR_STATESTORE_COMPONENT.read_text().splitlines()
            if line.strip().startswith("type:")
        }
        assert types == {"state.sqlite"}, (
            "SDR statestore must declare a durable `type: state.sqlite` "
            f"(not state.in-memory); found {types}"
        )
