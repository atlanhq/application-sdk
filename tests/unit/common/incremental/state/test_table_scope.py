"""Tests for table scope utilities (table_scope.py).

Tests cover:
- TableScope helpers (add/get/length/iter/close)
- get_current_table_scope: full path including DuckDB JSON loading + state counts
- get_table_qns_from_columns: parent table QN extraction from column files
- Error / edge cases: missing dir, empty dir, no qualifiedName, exception path

DuckDB :memory: is used (matches existing test_incremental_diff.py pattern).
RocksDB is replaced by passing dict via TableScope(table_states={}).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.common.incremental.models import TableScope
from application_sdk.common.incremental.state.table_scope import (
    add_table_to_scope,
    close_scope,
    get_current_table_scope,
    get_scope_length,
    get_table_qns_from_columns,
    get_table_state,
    iter_scope_table_qns,
)


def _scope() -> TableScope:
    """A TableScope backed by a plain dict — no real RocksDB."""
    return TableScope(table_states={})


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _table(qn: str, state: str | None = "CREATED") -> dict:
    return {
        "typeName": "Table",
        "attributes": {"qualifiedName": qn, "name": qn},
        "customAttributes": (
            json.dumps({"incremental_state": state}) if state else "{}"
        ),
    }


def _column(qn: str, table_qn: str | None = None, view_qn: str | None = None) -> dict:
    attrs: dict = {"qualifiedName": qn, "name": qn}
    if table_qn is not None:
        attrs["tableQualifiedName"] = table_qn
    if view_qn is not None:
        attrs["viewQualifiedName"] = view_qn
    return {"typeName": "Column", "attributes": attrs, "customAttributes": "{}"}


# ---------------------------------------------------------------------------
# Helpers: add/get/length/iter/close
# ---------------------------------------------------------------------------


class TestAddTableToScope:
    def test_adds_qn_and_state(self):
        scope = _scope()
        add_table_to_scope(scope, "db/s/t1", "CREATED")
        assert "db/s/t1" in scope.table_qualified_names
        assert scope.table_states["db/s/t1"] == "CREATED"

    def test_overwrites_existing_state(self):
        scope = _scope()
        add_table_to_scope(scope, "db/s/t1", "CREATED")
        add_table_to_scope(scope, "db/s/t1", "UPDATED")
        assert scope.table_states["db/s/t1"] == "UPDATED"
        assert get_scope_length(scope) == 1


class TestGetTableState:
    def test_returns_state_for_known_table(self):
        scope = _scope()
        add_table_to_scope(scope, "db/s/t1", "UPDATED")
        assert get_table_state(scope, "db/s/t1") == "UPDATED"

    def test_returns_none_for_unknown_table(self):
        scope = _scope()
        assert get_table_state(scope, "db/s/missing") is None

    def test_uses_key_may_exist_when_available(self):
        """If table_states has key_may_exist (RocksDB), it is consulted first."""

        class _FakeRdict(dict):
            def __init__(self) -> None:
                super().__init__()
                self.key_may_exist_calls: list[str] = []

            def key_may_exist(self, key: str) -> bool:
                self.key_may_exist_calls.append(key)
                return key in self

        fake = _FakeRdict()
        fake["db/s/t1"] = "CREATED"
        scope = TableScope(table_states=fake)
        scope.table_qualified_names.add("db/s/t1")

        assert get_table_state(scope, "db/s/t1") == "CREATED"
        assert get_table_state(scope, "db/s/missing") is None
        # Both lookups should have consulted the bloom filter.
        assert fake.key_may_exist_calls == ["db/s/t1", "db/s/missing"]


class TestScopeLengthAndIter:
    def test_get_scope_length_empty(self):
        assert get_scope_length(_scope()) == 0

    def test_get_scope_length_counts_qns(self):
        scope = _scope()
        add_table_to_scope(scope, "db/s/t1", "CREATED")
        add_table_to_scope(scope, "db/s/t2", "UPDATED")
        assert get_scope_length(scope) == 2

    def test_iter_scope_table_qns_yields_all(self):
        scope = _scope()
        add_table_to_scope(scope, "db/s/t1", "CREATED")
        add_table_to_scope(scope, "db/s/t2", "UPDATED")
        assert set(iter_scope_table_qns(scope)) == {"db/s/t1", "db/s/t2"}

    def test_iter_scope_returns_iterator(self):
        scope = _scope()
        add_table_to_scope(scope, "db/s/t1", "CREATED")
        it = iter_scope_table_qns(scope)
        # Should be an iterator (consumable)
        assert iter(it) is it
        assert next(it) == "db/s/t1"


class TestCloseScope:
    def test_close_scope_delegates_to_close_states_db(self):
        scope = _scope()
        with patch(
            "application_sdk.common.incremental.state.table_scope.close_states_db"
        ) as mock_close:
            close_scope(scope)
        mock_close.assert_called_once_with(scope.table_states)


# ---------------------------------------------------------------------------
# get_current_table_scope
# ---------------------------------------------------------------------------


class TestGetCurrentTableScope:
    def test_returns_none_when_table_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            transformed = Path(tmp) / "transformed"
            transformed.mkdir()
            assert get_current_table_scope(transformed) is None

    def test_returns_none_when_table_dir_has_no_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            transformed = Path(tmp) / "transformed"
            (transformed / "table").mkdir(parents=True)
            assert get_current_table_scope(transformed) is None

    def test_loads_tables_with_explicit_states(self):
        """Tables with customAttributes.incremental_state are loaded as-is."""
        with (
            patch(
                "application_sdk.common.incremental.state.table_scope.TableScope",
                side_effect=lambda: TableScope(table_states={}),
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            transformed = Path(tmp) / "transformed"
            _write_jsonl(
                transformed / "table" / "chunk-0.json",
                [
                    _table("db/s/t1", "CREATED"),
                    _table("db/s/t2", "UPDATED"),
                    _table("db/s/t3", "NO CHANGE"),
                ],
            )
            scope = get_current_table_scope(transformed)

        assert scope is not None
        assert get_scope_length(scope) == 3
        assert scope.table_states["db/s/t1"] == "CREATED"
        assert scope.state_counts == {"CREATED": 1, "UPDATED": 1, "NO CHANGE": 1}

    def test_falls_back_to_default_state_when_missing(self):
        """Missing customAttributes → default state INCREMENTAL_DEFAULT_STATE."""
        with (
            patch(
                "application_sdk.common.incremental.state.table_scope.TableScope",
                side_effect=lambda: TableScope(table_states={}),
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            transformed = Path(tmp) / "transformed"
            _write_jsonl(
                transformed / "table" / "chunk-0.json",
                [_table("db/s/t1", state=None)],
            )
            scope = get_current_table_scope(transformed)

        assert scope is not None
        # Default state is "NO CHANGE"
        assert scope.table_states["db/s/t1"] == "NO CHANGE"

    def test_drops_rows_with_null_qualified_name(self):
        """Rows with null qualifiedName are filtered out via DELETE."""
        with (
            patch(
                "application_sdk.common.incremental.state.table_scope.TableScope",
                side_effect=lambda: TableScope(table_states={}),
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            transformed = Path(tmp) / "transformed"
            _write_jsonl(
                transformed / "table" / "chunk-0.json",
                [
                    _table("db/s/t1", "CREATED"),
                    # Row with no qualifiedName attribute
                    {
                        "typeName": "Table",
                        "attributes": {"name": "ghost"},
                        "customAttributes": "{}",
                    },
                ],
            )
            scope = get_current_table_scope(transformed)

        assert scope is not None
        assert get_scope_length(scope) == 1
        assert "db/s/t1" in scope.table_qualified_names

    def test_failure_closes_scope_and_raises_typed_error(self):
        """Any DuckDB error is raised as TableScopeLoadError and scope closed."""
        with (
            patch(
                "application_sdk.common.incremental.state.table_scope.TableScope",
                side_effect=lambda: TableScope(table_states={}),
            ),
            patch(
                "application_sdk.common.incremental.state.table_scope.close_states_db"
            ) as mock_close,
        ):
            with (
                patch(
                    "application_sdk.common.incremental.state.table_scope."
                    "managed_duckdb_connection",
                    side_effect=RuntimeError("boom"),
                ),
                tempfile.TemporaryDirectory() as tmp,
            ):
                transformed = Path(tmp) / "transformed"
                (transformed / "table").mkdir(parents=True)
                (transformed / "table" / "chunk-0.json").write_text(
                    json.dumps(_table("db/s/t1", "CREATED"))
                )
                from application_sdk.common.incremental.incremental_errors import (
                    TableScopeLoadError,
                )

                with pytest.raises(TableScopeLoadError) as excinfo:
                    get_current_table_scope(transformed)
                assert excinfo.value.code == "INTERNAL_INCREMENTAL_TABLE_SCOPE"
            mock_close.assert_called_once()


# ---------------------------------------------------------------------------
# get_table_qns_from_columns
# ---------------------------------------------------------------------------


class TestGetTableQnsFromColumns:
    def test_returns_none_when_dir_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert get_table_qns_from_columns(Path(tmp) / "missing") is None

    def test_returns_none_when_no_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            col_dir = Path(tmp) / "column"
            col_dir.mkdir()
            assert get_table_qns_from_columns(col_dir) is None

    def test_extracts_table_qn(self):
        with tempfile.TemporaryDirectory() as tmp:
            col_dir = Path(tmp) / "column"
            _write_jsonl(
                col_dir / "chunk-0.json",
                [
                    _column("db/s/t1.c1", table_qn="db/s/t1"),
                    _column("db/s/t1.c2", table_qn="db/s/t1"),
                    _column("db/s/t2.c1", table_qn="db/s/t2"),
                ],
            )
            qns = get_table_qns_from_columns(col_dir)
        assert qns == {"db/s/t1", "db/s/t2"}

    def test_falls_back_to_view_qn(self):
        """When tableQualifiedName is null, viewQualifiedName is used."""
        with tempfile.TemporaryDirectory() as tmp:
            col_dir = Path(tmp) / "column"
            _write_jsonl(
                col_dir / "chunk-0.json",
                [_column("v1.c1", view_qn="db/s/v1")],
            )
            qns = get_table_qns_from_columns(col_dir)
        assert qns == {"db/s/v1"}

    def test_returns_none_when_no_parent_qns_found(self):
        """All rows missing table/view qn → returns None and logs warning."""
        with tempfile.TemporaryDirectory() as tmp:
            col_dir = Path(tmp) / "column"
            _write_jsonl(col_dir / "chunk-0.json", [_column("orphan")])
            qns = get_table_qns_from_columns(col_dir)
        assert qns is None

    def test_returns_none_on_duckdb_error(self):
        """DuckDB errors are caught and None is returned (logged)."""
        with (
            patch(
                "application_sdk.common.incremental.state.table_scope."
                "managed_duckdb_connection",
                side_effect=RuntimeError("explode"),
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            col_dir = Path(tmp) / "column"
            _write_jsonl(col_dir / "chunk-0.json", [_column("c1", "db/s/t1")])
            qns = get_table_qns_from_columns(col_dir)
        assert qns is None
