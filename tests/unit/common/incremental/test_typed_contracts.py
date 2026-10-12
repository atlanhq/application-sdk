"""The typed incremental contracts, and the compatibility they promise.

Covers the StrEnums (``TableState``, ``EntityType``, ``ColumnBatchStatus``),
the typed marker API (``fetch_marker`` / ``persist_marker``) and its deprecated
tuple/dict shims, the single ``None`` <-> ``""`` marker boundary, the states
store and DuckDB Protocols, and the sequential carry-forward copy.

The compatibility tests compare against literal legacy values rather than
against the new code's own output, so they fail if the wire shape drifts.
"""

from __future__ import annotations

import threading
import warnings
from enum import StrEnum
from pathlib import Path
from unittest.mock import AsyncMock, patch

import orjson
import pytest

from application_sdk.common.incremental import helpers, marker
from application_sdk.common.incremental.models import EntityType, TableScope, TableState
from application_sdk.common.incremental.state.table_scope import (
    add_table_to_scope,
    get_table_state,
)
from application_sdk.common.incremental.storage import duckdb_utils
from application_sdk.common.incremental.storage.rocksdb_utils import (
    ProbeableStatesStore,
    RocksStatesStore,
    close_states_db,
)
from application_sdk.constants import (
    COLUMN_BATCHES_SUBPATH,
    CURRENT_STATE_SUBPATH,
    INCREMENTAL_DEFAULT_STATE,
    MARKER_FILENAME,
    TRANSFORMED_SUBDIR,
)
from application_sdk.templates.contracts.incremental_sql import (
    ColumnBatchStatus,
    ExecuteColumnBatchOutput,
    IncrementalTaskInput,
    marker_from_wire,
    marker_to_wire,
)

CONN_QN = "default/oracle/1764230875"
APP = "oracle"

# ---------------------------------------------------------------------------
# StrEnums: identical to the strings they replaced
# ---------------------------------------------------------------------------

#: The literal strings each enum replaced. Written out, not derived from the
#: enums, so a changed value fails here.
_LEGACY_VALUES: list[tuple[StrEnum, str]] = [
    (TableState.CREATED, "CREATED"),
    (TableState.UPDATED, "UPDATED"),
    (TableState.NO_CHANGE, "NO CHANGE"),
    (TableState.BACKFILL, "BACKFILL"),
    (EntityType.TABLE, "table"),
    (EntityType.COLUMN, "column"),
    (EntityType.SCHEMA, "schema"),
    (EntityType.DATABASE, "database"),
    (ColumnBatchStatus.SUCCESS, "success"),
    (ColumnBatchStatus.NOT_FOUND, "not_found"),
]


@pytest.mark.parametrize(("member", "legacy"), _LEGACY_VALUES)
def test_member_is_interchangeable_with_its_legacy_string(
    member: StrEnum, legacy: str
) -> None:
    assert member == legacy
    assert hash(member) == hash(legacy)
    assert str(member) == legacy
    assert f"{member}" == legacy
    assert orjson.dumps(member) == orjson.dumps(legacy)
    assert {legacy: 1}.get(member) == 1


def test_default_state_constant_is_the_no_change_member() -> None:
    assert INCREMENTAL_DEFAULT_STATE == TableState.NO_CHANGE


def test_column_batch_output_round_trips_like_the_old_string_field() -> None:
    """A payload recorded with the old ``str`` field reads back, and writes the same JSON."""
    for legacy in ("success", "not_found"):
        recorded = orjson.dumps({"batch_index": 2, "records": 7, "status": legacy})
        out = ExecuteColumnBatchOutput.model_validate_json(recorded)
        assert out.status is ColumnBatchStatus(legacy)
        assert out.status == legacy
        assert orjson.loads(out.model_dump_json())["status"] == legacy


def test_column_batch_output_reads_a_legacy_unset_status_as_none() -> None:
    """The old default ``""`` meant "not set"; it must still deserialize on replay."""
    recorded = orjson.dumps({"batch_index": 0, "records": 0, "status": ""})
    assert ExecuteColumnBatchOutput.model_validate_json(recorded).status is None
    assert ExecuteColumnBatchOutput().status is None


def test_column_batch_output_writes_an_unset_status_as_empty_string() -> None:
    """Unset is ``None`` in Python but ``""`` on the wire, exactly as before."""
    out = ExecuteColumnBatchOutput()
    assert out.status is None
    assert orjson.loads(out.model_dump_json())["status"] == ""
    assert out.model_dump()["status"] == ""
    assert out.model_dump(mode="json")["status"] == ""
    # And a set status still writes its plain string, in every mode.
    done = ExecuteColumnBatchOutput(status=ColumnBatchStatus.SUCCESS)
    assert (
        out.model_validate_json(done.model_dump_json()).status
        is ColumnBatchStatus.SUCCESS
    )
    assert type(done.model_dump()["status"]) is str


async def test_column_batch_status_round_trips_through_the_temporal_converter() -> None:
    """The payload Temporal records carries ``""`` / the plain string, and reads back."""
    from temporalio.contrib.pydantic import pydantic_data_converter as converter

    for out, wire in (
        (ExecuteColumnBatchOutput(), ""),
        (ExecuteColumnBatchOutput(status=ColumnBatchStatus.NOT_FOUND), "not_found"),
    ):
        [payload] = await converter.encode([out])
        assert orjson.loads(payload.data)["status"] == wire
        [back] = await converter.decode([payload], [ExecuteColumnBatchOutput])
        assert back.status == out.status


def test_column_batch_output_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError):
        ExecuteColumnBatchOutput(status="done")  # type: ignore[arg-type]


def test_table_state_stored_in_scope_reads_back_as_the_wire_string() -> None:
    scope = TableScope(table_states={})
    add_table_to_scope(scope, "db/s/t1", TableState.NO_CHANGE)
    add_table_to_scope(scope, "db/s/t2", "UPDATED")  # as read from JSON
    assert get_table_state(scope, "db/s/t1") == "NO CHANGE"
    assert get_table_state(scope, "db/s/t2") == TableState.UPDATED


# ---------------------------------------------------------------------------
# Path constants: the literals they replaced
# ---------------------------------------------------------------------------


def test_path_constants_keep_their_legacy_values() -> None:
    assert CURRENT_STATE_SUBPATH == "current-state"
    assert MARKER_FILENAME == "marker.txt"
    assert TRANSFORMED_SUBDIR == "transformed"
    assert COLUMN_BATCHES_SUBPATH == "batches/column-table-ids"


# ---------------------------------------------------------------------------
# Typed marker API
# ---------------------------------------------------------------------------


class TestFetchMarker:
    async def test_first_run_has_no_marker(self) -> None:
        with patch.object(
            marker, "download_marker_from_s3", new=AsyncMock(return_value=None)
        ):
            markers = await marker.fetch_marker(CONN_QN, APP)
        assert isinstance(markers, marker.MarkerPair)
        assert markers.marker is None
        assert markers.next_marker

    async def test_stored_marker_is_processed(self) -> None:
        with patch.object(
            marker,
            "download_marker_from_s3",
            new=AsyncMock(return_value="2025-01-15T10:30:00.123Z"),
        ):
            markers = await marker.fetch_marker(
                CONN_QN, APP, prepone_enabled=True, prepone_hours=3
            )
        assert markers.marker == "2025-01-15T07:30:00Z"

    async def test_empty_existing_marker_falls_back_to_storage(self) -> None:
        """``""`` is the wire form of "no marker", never a marker itself."""
        download = AsyncMock(return_value="2025-01-15T10:30:00Z")
        with patch.object(marker, "download_marker_from_s3", new=download):
            markers = await marker.fetch_marker(CONN_QN, APP, existing_marker="")
        download.assert_awaited_once()
        assert markers.marker == "2025-01-15T10:30:00Z"


class TestPersistMarker:
    async def test_returns_the_value_and_key_written(self, memory_store) -> None:
        result = await marker.persist_marker(CONN_QN, "2025-01-15T10:00:00Z", APP)
        assert result == marker.MarkerPersistResult(
            marker_timestamp="2025-01-15T10:00:00Z",
            s3_key=(
                "persistent-artifacts/apps/oracle/connection/1764230875/marker.txt"
            ),
        )
        assert await helpers.download_marker_from_s3(CONN_QN, APP) == (
            "2025-01-15T10:00:00Z"
        )


# ---------------------------------------------------------------------------
# Deprecated marker shims: byte-identical shapes, plus a DeprecationWarning
# ---------------------------------------------------------------------------


class TestLegacyMarkerShims:
    async def test_fetch_returns_the_exact_legacy_tuple_and_warns(self) -> None:
        with (
            patch.object(
                marker,
                "download_marker_from_s3",
                new=AsyncMock(return_value="2025-01-15T10:30:00Z"),
            ),
            patch.object(
                marker, "create_next_marker", return_value="2025-02-01T00:00:00Z"
            ),
            pytest.warns(DeprecationWarning, match="use fetch_marker") as caught,
        ):
            result = await marker.fetch_marker_from_storage(CONN_QN, APP)

        assert type(result) is tuple
        assert result == ("2025-01-15T10:30:00Z", "2025-02-01T00:00:00Z")
        assert "v4.0.0" in str(caught[0].message)
        # The warning names the caller, not the shim.
        assert caught[0].filename == __file__

    async def test_fetch_first_run_tuple_carries_none(self) -> None:
        with (
            patch.object(
                marker, "download_marker_from_s3", new=AsyncMock(return_value=None)
            ),
            patch.object(
                marker, "create_next_marker", return_value="2025-02-01T00:00:00Z"
            ),
            pytest.warns(DeprecationWarning),
        ):
            result = await marker.fetch_marker_from_storage(CONN_QN, APP)
        assert result == (None, "2025-02-01T00:00:00Z")

    async def test_persist_returns_the_exact_legacy_dict_and_warns(
        self, memory_store
    ) -> None:
        with pytest.warns(DeprecationWarning, match="use persist_marker") as caught:
            result = await marker.persist_marker_to_storage(
                CONN_QN, "2025-01-15T10:00:00Z", APP
            )

        # The same keys, values, types and key order as before.
        assert type(result) is dict
        assert orjson.dumps(result) == orjson.dumps(
            {
                "marker_written": True,
                "marker_timestamp": "2025-01-15T10:00:00Z",
                "local_path": "",
                "s3_key": (
                    "persistent-artifacts/apps/oracle/connection/1764230875/marker.txt"
                ),
            }
        )
        # What a legacy caller reads with .get().
        assert result.get("s3_key", "").endswith("/marker.txt")
        assert "v4.0.0" in str(caught[0].message)

    async def test_typed_api_emits_no_deprecation_warning(self, memory_store) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            await marker.persist_marker(CONN_QN, "2025-01-15T10:00:00Z", APP)
            await marker.fetch_marker(CONN_QN, APP)


# ---------------------------------------------------------------------------
# One marker sentinel: None in Python, "" on the wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("python", "wire"),
    [(None, ""), ("2025-01-15T10:00:00Z", "2025-01-15T10:00:00Z")],
)
def test_marker_boundary_round_trips(python: str | None, wire: str) -> None:
    assert marker_to_wire(python) == wire
    assert marker_from_wire(wire) == python
    assert marker_from_wire(marker_to_wire(python)) == python


def test_task_contract_still_carries_the_empty_string() -> None:
    """The wire form is unchanged: a task input with no marker holds ``""``."""
    payload = IncrementalTaskInput(marker_timestamp=marker_to_wire(None))
    assert orjson.loads(payload.model_dump_json())["marker_timestamp"] == ""


# ---------------------------------------------------------------------------
# States store Protocols
# ---------------------------------------------------------------------------


class _FakeRdict(dict[str, str]):
    """A dict with the three Rdict methods the incremental code calls."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = path
        self.closed = False

    def key_may_exist(self, key: str) -> bool:
        return key in self

    def path(self) -> str:
        return str(self._path)

    def close(self) -> None:
        self.closed = True


def test_a_plain_dict_is_a_states_store_but_not_a_rocks_store() -> None:
    assert not isinstance({}, ProbeableStatesStore)
    assert not isinstance({}, RocksStatesStore)
    close_states_db({})  # nothing to close; must not raise


def test_close_states_db_closes_and_removes_a_rocks_store(tmp_path: Path) -> None:
    db_dir = tmp_path / "states"
    db_dir.mkdir()
    db = _FakeRdict(db_dir)
    assert isinstance(db, RocksStatesStore)
    close_states_db(db)
    assert db.closed
    assert not db_dir.exists()


# ---------------------------------------------------------------------------
# DuckDB Protocol and row helpers
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows

    def fetchone(self) -> tuple[object, ...] | None:
        return self._rows[0] if self._rows else None


def test_fetch_count_reads_the_count_or_zero() -> None:
    assert duckdb_utils.fetch_count(_Result([(5,)])) == 5
    assert duckdb_utils.fetch_count(_Result([])) == 0
    with pytest.raises(TypeError):
        duckdb_utils.fetch_count(_Result([("5",)]))


def test_fetch_str_set_skips_nulls() -> None:
    rows: list[tuple[object, ...]] = [("a",), (None,), ("b",), ("a",)]
    assert duckdb_utils.fetch_str_set(_Result(rows)) == {"a", "b"}


def test_a_real_connection_satisfies_the_protocol() -> None:
    with duckdb_utils.managed_duckdb_connection() as conn:
        assert (
            duckdb_utils.fetch_count(conn.execute("SELECT COUNT(*) FROM range(3)")) == 3
        )


# ---------------------------------------------------------------------------
# Sequential carry-forward copy
# ---------------------------------------------------------------------------


def test_copy_directory_parallel_copies_on_the_calling_thread(tmp_path: Path) -> None:
    """No nested pool: every copy runs on the (already offloaded) caller's thread."""
    src = tmp_path / "src"
    src.mkdir()
    for i in range(4):
        (src / f"chunk-{i}.json").write_text(f'{{"i": {i}}}')

    seen_threads: set[int] = set()
    real_copy = helpers.atomic_copy

    def _recording_copy(*args: object, **kwargs: object) -> None:
        seen_threads.add(threading.get_ident())
        real_copy(*args, **kwargs)  # type: ignore[arg-type]

    with patch.object(helpers, "atomic_copy", side_effect=_recording_copy):
        count = helpers.copy_directory_parallel(src, tmp_path / "dest", max_workers=8)

    assert count == 4
    assert seen_threads == {threading.get_ident()}
    assert sorted(p.name for p in (tmp_path / "dest").glob("*.json")) == [
        f"chunk-{i}.json" for i in range(4)
    ]
