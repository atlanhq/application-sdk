"""Tests for DuckDB backfill detection (column_extraction/backfill.py).

Tests cover:
- get_backfill_tables: full path comparing current vs previous transformed dirs.
- _load_tables_to_duckdb: Empty-file branch, multi-file UNION ALL branch,
  inline ``import duckdb`` (BLDX-1129 anchor).
- Failure modes: bare ``except Exception`` swallowing — flagged as a bug
  but covered for behaviour.

Uses real in-memory DuckDB (matches the existing test_incremental_diff.py
pattern). No real RocksDB / S3 / threads.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.common.incremental.column_extraction.backfill import (
    _load_tables_to_duckdb,
    get_backfill_tables,
)


def _duckdb_or_skip():
    """Lazy import of duckdb (top-level import breaks under coverage tracing
    because of duckdb's native ``_duckdb`` extension package)."""
    try:
        import duckdb
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"duckdb unavailable: {exc}")
    return duckdb


def _connect():
    return _duckdb_or_skip().connect(":memory:")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_record(qn: str, type_name: str = "Table") -> dict:
    return {
        "typeName": type_name,
        "attributes": {"qualifiedName": qn, "name": qn},
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# get_backfill_tables — top-level branches
# ---------------------------------------------------------------------------


class TestGetBackfillTablesGuards:
    def test_returns_none_when_previous_state_is_none(self):
        """First run (no previous state) returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            current.mkdir()
            assert get_backfill_tables(current, None) is None

    def test_returns_none_when_previous_state_does_not_exist(self):
        """Previous state path that doesn't exist returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            previous = Path(tmp) / "missing"
            current.mkdir()
            assert get_backfill_tables(current, previous) is None


class TestGetBackfillTablesDiff:
    """Full DuckDB path: previous exists, current has files."""

    def test_no_current_tables_swallowed_by_bare_except(self):
        """No current tables → ValueError is raised but caught by the bare
        except in get_backfill_tables, returning None.

        NOTE: Bare except swallows programming errors silently — flagged
        as a bug ('Bugs / issues found' in the report).
        """
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            previous = Path(tmp) / "previous"
            (current / "table").mkdir(parents=True)
            (previous / "table").mkdir(parents=True)
            _write_jsonl(
                previous / "table" / "chunk-0.json",
                [_table_record("db/s/t1")],
            )
            assert get_backfill_tables(current, previous) is None

    def test_no_previous_tables_returns_none(self):
        """Previous dir exists but is empty → returns None (cannot diff)."""
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            previous = Path(tmp) / "previous"
            _write_jsonl(
                current / "table" / "chunk-0.json",
                [_table_record("db/s/t1")],
            )
            (previous / "table").mkdir(parents=True)
            assert get_backfill_tables(current, previous) is None

    def test_returns_tables_in_current_but_not_previous(self):
        """Diff: tables present in current but absent in previous → backfill."""
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            previous = Path(tmp) / "previous"
            _write_jsonl(
                current / "table" / "chunk-0.json",
                [
                    _table_record("db/s/t1"),
                    _table_record("db/s/t2"),
                    _table_record("db/s/new"),
                ],
            )
            _write_jsonl(
                previous / "table" / "chunk-0.json",
                [
                    _table_record("db/s/t1"),
                    _table_record("db/s/t2"),
                ],
            )
            result = get_backfill_tables(current, previous)
        assert result == {"db/s/new"}

    def test_returns_empty_set_when_all_present(self):
        """All current tables present in previous → empty set (NOT None)."""
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            previous = Path(tmp) / "previous"
            for d in (current, previous):
                _write_jsonl(
                    d / "table" / "chunk-0.json",
                    [_table_record("db/s/t1")],
                )
            result = get_backfill_tables(current, previous)
        assert result == set()

    def test_type_name_distinguishes_tables_with_same_qn(self):
        """Diff matches on (qualified_name, type_name) — same qn but
        different type_name should still be flagged for backfill."""
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current"
            previous = Path(tmp) / "previous"
            _write_jsonl(
                current / "table" / "chunk-0.json",
                [_table_record("db/s/x", type_name="View")],
            )
            _write_jsonl(
                previous / "table" / "chunk-0.json",
                [_table_record("db/s/x", type_name="Table")],
            )
            result = get_backfill_tables(current, previous)
        assert result == {"db/s/x"}

    def test_propagates_duckdb_failure(self):
        """DuckDB / runtime failures must propagate, not be swallowed.

        Locks the BLDX-1190 fix on PR #1609: the bare ``except Exception``
        was narrowed to ``except ValueError`` so DuckDB / SQL / disk
        errors surface to the caller instead of returning silent ``None``.
        """
        with (
            patch(
                "application_sdk.common.incremental.column_extraction.backfill."
                "DuckDBConnectionManager",
                side_effect=RuntimeError("boom"),
            ),
            tempfile.TemporaryDirectory() as tmp,
        ):
            current = Path(tmp) / "current"
            previous = Path(tmp) / "previous"
            _write_jsonl(
                current / "table" / "chunk-0.json",
                [_table_record("db/s/t1")],
            )
            _write_jsonl(
                previous / "table" / "chunk-0.json",
                [_table_record("db/s/t1")],
            )
            with pytest.raises(RuntimeError, match="boom"):
                get_backfill_tables(current, previous)


# ---------------------------------------------------------------------------
# _load_tables_to_duckdb — direct
# ---------------------------------------------------------------------------


class TestLoadTablesToDuckDB:
    def test_no_table_dir_creates_empty_table_returns_none(self):
        """No table dir → still creates the DuckDB table (empty), returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            base.mkdir()
            conn = _connect()
            try:
                count = _load_tables_to_duckdb(conn, base, "t_empty")
                assert count is None
                # Empty schema must exist
                schema = conn.execute("DESCRIBE t_empty").fetchall()
                col_names = {row[0] for row in schema}
                assert "type_name" in col_names
                assert "qualified_name" in col_names
            finally:
                conn.close()

    def test_no_json_files_creates_empty_table_returns_none(self):
        """Empty table dir → empty schema returned, no rows."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "table").mkdir(parents=True)
            conn = _connect()
            try:
                count = _load_tables_to_duckdb(conn, base, "t_empty2")
                assert count is None
                # Verify the table is empty
                count_row = conn.execute("SELECT COUNT(*) FROM t_empty2").fetchone()
                assert count_row[0] == 0
            finally:
                conn.close()

    def test_loads_records_from_single_file(self):
        """Single JSON file → records are loaded into DuckDB table."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            _write_jsonl(
                base / "table" / "chunk-0.json",
                [
                    _table_record("db/s/t1"),
                    _table_record("db/s/t2"),
                ],
            )
            conn = _connect()
            try:
                count = _load_tables_to_duckdb(conn, base, "t_loaded")
                assert count == 2
                rows = conn.execute(
                    "SELECT type_name, qualified_name FROM t_loaded ORDER BY qualified_name"
                ).fetchall()
                assert rows == [
                    ("Table", "db/s/t1"),
                    ("Table", "db/s/t2"),
                ]
            finally:
                conn.close()

    def test_loads_records_from_multiple_files_union(self):
        """Multiple files → UNION ALL combines them."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            _write_jsonl(base / "table" / "chunk-0.json", [_table_record("db/s/t1")])
            _write_jsonl(base / "table" / "chunk-1.json", [_table_record("db/s/t2")])
            conn = _connect()
            try:
                count = _load_tables_to_duckdb(conn, base, "t_multi")
                assert count == 2
            finally:
                conn.close()

    def test_filters_records_with_null_qn(self):
        """Rows where typeName or qualifiedName is null are filtered by WHERE."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            _write_jsonl(
                base / "table" / "chunk-0.json",
                [
                    _table_record("db/s/t1"),
                    # Row with null qualifiedName -> excluded
                    {"typeName": "Table", "attributes": {"name": "ghost"}},
                ],
            )
            conn = _connect()
            try:
                count = _load_tables_to_duckdb(conn, base, "t_filter")
                assert count == 1
            finally:
                conn.close()

    def test_handles_qn_with_single_quote(self):
        """Filenames with single quotes are escaped before embedding in SQL."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "table").mkdir(parents=True)
            quoted = base / "table" / "chunk's-0.json"
            _write_jsonl(quoted, [_table_record("db/s/t1")])
            conn = _connect()
            try:
                count = _load_tables_to_duckdb(conn, base, "t_quoted")
                assert count == 1
            finally:
                conn.close()

    def test_oserror_during_glob_raises_json_scan_error(self):
        """OSError when scanning JSON files is raised as JsonScanError."""
        from application_sdk.common.incremental.incremental_errors import JsonScanError

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "table").mkdir(parents=True)
            conn = _connect()
            try:
                with patch.object(Path, "glob", side_effect=OSError("disk gone")):
                    with pytest.raises(JsonScanError) as excinfo:
                        _load_tables_to_duckdb(conn, base, "t_oserr")
                assert excinfo.value.code == "INTERNAL_INCREMENTAL_JSON_SCAN"
            finally:
                conn.close()

    def test_duckdb_error_during_load_is_logged_and_raised(self):
        """A duckdb.Error during CREATE TABLE is logged + re-raised.

        This also exercises the inline ``import duckdb`` (BLDX-1129 anchor).
        """
        duckdb_mod = _duckdb_or_skip()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            _write_jsonl(base / "table" / "chunk-0.json", [_table_record("db/s/t1")])
            conn = _connect()
            try:
                # First CREATE succeeds; second will fail with duckdb.Error
                # because the table already exists. We use the same name twice.
                _load_tables_to_duckdb(conn, base, "t_collide")
                with pytest.raises(duckdb_mod.Error):
                    _load_tables_to_duckdb(conn, base, "t_collide")
            finally:
                conn.close()
