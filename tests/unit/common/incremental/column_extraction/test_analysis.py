"""Unit tests for incremental.column_extraction.analysis.

Uses real DuckDB on tiny tmp_path JSON files. No real I/O outside tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from application_sdk.common.incremental.column_extraction.analysis import (
    get_tables_needing_column_extraction,
    get_transformed_dir,
)
from application_sdk.common.incremental.incremental_errors import (
    ColumnExtractionAnalysisError,
    DaftAnalysisError,
)


def _duckdb_or_skip():
    """Lazy import of duckdb (top-level import breaks under coverage tracing
    because of duckdb's native ``_duckdb`` extension package)."""
    try:
        import duckdb  # noqa: F401
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"duckdb unavailable: {exc}")


def _write_table_json(out: Path, name: str, qualified_name: str, state: str) -> None:
    """Write one table-record JSON file in the format expected by analysis."""
    record = {
        "typeName": "Table",
        "attributes": {
            "databaseName": "db",
            "schemaName": "sch",
            "name": name,
            "qualifiedName": qualified_name,
        },
        "customAttributes": {"incremental_state": state},
    }
    out.write_text(json.dumps(record))


# ---------------------------------------------------------------------------
# get_transformed_dir
# ---------------------------------------------------------------------------


class TestGetTransformedDir:
    def test_missing_output_path_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            get_transformed_dir({})

    def test_empty_output_path_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            get_transformed_dir({"output_path": ""})

    def test_whitespace_output_path_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            get_transformed_dir({"output_path": "   "})

    def test_missing_transformed_dir_raises(self, tmp_path: Path) -> None:
        # output_path exists but no `transformed/` subdir
        with pytest.raises(FileNotFoundError):
            get_transformed_dir({"output_path": str(tmp_path)})

    def test_empty_transformed_dir_raises(self, tmp_path: Path) -> None:
        # transformed/ exists but no JSON files in it
        (tmp_path / "transformed").mkdir()
        with pytest.raises(FileNotFoundError):
            get_transformed_dir({"output_path": str(tmp_path)})

    def test_returns_path_when_json_exists(self, tmp_path: Path) -> None:
        (tmp_path / "transformed").mkdir()
        (tmp_path / "transformed" / "x.json").write_text("{}")
        result = get_transformed_dir({"output_path": str(tmp_path)})
        assert result == tmp_path / "transformed"


# ---------------------------------------------------------------------------
# get_tables_needing_column_extraction
# ---------------------------------------------------------------------------


class TestGetTablesNeedingColumnExtraction:
    """Tests for the DuckDB-based table state analysis."""

    def test_missing_table_subdir_raises(self, tmp_path: Path) -> None:
        _duckdb_or_skip()
        # transformed/ but no transformed/table/ subdir
        (tmp_path / "transformed").mkdir()
        with pytest.raises(
            Exception
        ):  # ColumnExtractionAnalysisError wraps FileNotFoundError
            get_tables_needing_column_extraction(tmp_path / "transformed")

    def test_empty_table_subdir_raises(self, tmp_path: Path) -> None:
        _duckdb_or_skip()
        (tmp_path / "transformed" / "table").mkdir(parents=True)
        with pytest.raises(Exception):
            get_tables_needing_column_extraction(tmp_path / "transformed")

    def test_changed_tables_only(self, tmp_path: Path) -> None:
        """One CREATED, one UPDATED, one NO CHANGE → 2 changed, 1 unchanged."""
        _duckdb_or_skip()
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "CREATED")
        _write_table_json(td / "b.json", "b", "db.sch.b", "UPDATED")
        _write_table_json(td / "c.json", "c", "db.sch.c", "NO CHANGE")

        rows, changed, backfill, no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed"
        )
        assert changed == 2
        assert backfill == 0
        assert no_change == 1
        # Filtered list must contain only the 2 changed rows
        assert len(rows) == 2
        table_ids = {r["table_id"] for r in rows}
        assert table_ids == {"db.sch.a", "db.sch.b"}
        assert all(r["is_changed"] for r in rows)
        assert all(not r["is_backfill"] for r in rows)

    def test_with_backfill_qns_includes_unchanged_matching(
        self, tmp_path: Path
    ) -> None:
        """Tables that are NO CHANGE but in backfill_qns should be marked is_backfill."""
        _duckdb_or_skip()
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "NO CHANGE")
        _write_table_json(td / "b.json", "b", "db.sch.b", "CREATED")
        _write_table_json(td / "c.json", "c", "db.sch.c", "NO CHANGE")

        rows, changed, backfill, no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed",
            backfill_qualified_names={"db.sch.a"},
        )
        assert changed == 1  # b
        assert backfill == 1  # a (in backfill set, not changed)
        assert no_change == 2
        assert len(rows) == 2

        backfill_rows = [r for r in rows if r["is_backfill"]]
        changed_rows = [r for r in rows if r["is_changed"]]
        assert len(backfill_rows) == 1
        assert backfill_rows[0]["table_id"] == "db.sch.a"
        assert len(changed_rows) == 1
        assert changed_rows[0]["table_id"] == "db.sch.b"

    def test_with_empty_backfill_qns_uses_false_branch(self, tmp_path: Path) -> None:
        """When backfill_qns is empty/None, no rows marked is_backfill."""
        _duckdb_or_skip()
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "CREATED")

        rows, changed, backfill, no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed", backfill_qualified_names=None
        )
        assert changed == 1
        assert backfill == 0
        assert len(rows) == 1
        assert rows[0]["is_changed"] is True
        assert rows[0]["is_backfill"] is False

    def test_all_unchanged_returns_empty_list(self, tmp_path: Path) -> None:
        _duckdb_or_skip()
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "NO CHANGE")
        _write_table_json(td / "b.json", "b", "db.sch.b", "NO CHANGE")

        rows, changed, backfill, no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed"
        )
        assert changed == 0
        assert backfill == 0
        assert no_change == 2
        assert rows == []

    def test_row_dict_has_expected_keys(self, tmp_path: Path) -> None:
        """Each returned dict must have exactly table_id, is_changed, is_backfill."""
        _duckdb_or_skip()
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "CREATED")

        rows, _changed, _backfill, _no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed"
        )
        assert len(rows) == 1
        assert set(rows[0].keys()) == {"table_id", "is_changed", "is_backfill"}
        assert isinstance(rows[0]["is_changed"], bool)
        assert isinstance(rows[0]["is_backfill"], bool)


# ---------------------------------------------------------------------------
# Backward-compat alias
# ---------------------------------------------------------------------------


def test_daft_analysis_error_is_column_extraction_analysis_error() -> None:
    """DaftAnalysisError must remain a back-compat alias for ColumnExtractionAnalysisError."""
    assert DaftAnalysisError is ColumnExtractionAnalysisError
