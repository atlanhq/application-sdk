"""Unit tests for incremental.column_extraction.analysis.

Uses real Daft on tiny tmp_path JSON files. No real I/O outside tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from application_sdk.common.incremental.column_extraction.analysis import (
    get_tables_needing_column_extraction,
    get_transformed_dir,
)


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

    def test_missing_transformed_dir_raises(self, tmp_path) -> None:
        # output_path exists but no `transformed/` subdir
        with pytest.raises(FileNotFoundError):
            get_transformed_dir({"output_path": str(tmp_path)})

    def test_empty_transformed_dir_raises(self, tmp_path) -> None:
        # transformed/ exists but no JSON files in it
        (tmp_path / "transformed").mkdir()
        with pytest.raises(FileNotFoundError):
            get_transformed_dir({"output_path": str(tmp_path)})

    def test_returns_path_when_json_exists(self, tmp_path) -> None:
        (tmp_path / "transformed").mkdir()
        (tmp_path / "transformed" / "x.json").write_text("{}")
        result = get_transformed_dir({"output_path": str(tmp_path)})
        assert result == tmp_path / "transformed"


# ---------------------------------------------------------------------------
# get_tables_needing_column_extraction
# ---------------------------------------------------------------------------


class TestGetTablesNeedingColumnExtraction:
    """BLDX-1129 anchor: each test exercises function-local
    `import daft` and `from daft.functions import format`."""

    def test_missing_table_subdir_raises(self, tmp_path) -> None:
        # transformed/ but no transformed/table/ subdir
        (tmp_path / "transformed").mkdir()
        with pytest.raises(Exception):  # rewrap wraps FileNotFoundError
            get_tables_needing_column_extraction(tmp_path / "transformed")

    def test_empty_table_subdir_raises(self, tmp_path) -> None:
        (tmp_path / "transformed" / "table").mkdir(parents=True)
        with pytest.raises(Exception):
            get_tables_needing_column_extraction(tmp_path / "transformed")

    def test_changed_tables_only(self, tmp_path) -> None:
        """One CREATED, one UPDATED, one NO CHANGE → 2 changed, 1 unchanged."""
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "CREATED")
        _write_table_json(td / "b.json", "b", "db.sch.b", "UPDATED")
        _write_table_json(td / "c.json", "c", "db.sch.c", "NO CHANGE")

        df, changed, backfill, no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed"
        )
        assert changed == 2
        assert backfill == 0
        assert no_change == 1
        # Filtered DataFrame must contain only the 2 changed rows
        rows = list(df.iter_rows())
        assert len(rows) == 2

    def test_with_backfill_qns_includes_unchanged_matching(self, tmp_path) -> None:
        """Tables that are NO CHANGE but in backfill_qns should be marked is_backfill."""
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "NO CHANGE")
        _write_table_json(td / "b.json", "b", "db.sch.b", "CREATED")
        _write_table_json(td / "c.json", "c", "db.sch.c", "NO CHANGE")

        df, changed, backfill, no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed",
            backfill_qualified_names={"db.sch.a"},
        )
        assert changed == 1  # b
        assert backfill == 1  # a (in backfill set, not changed)
        assert no_change == 2

    def test_with_empty_backfill_qns_uses_lit_false_branch(self, tmp_path) -> None:
        """When backfill_qns is empty/None, no rows marked is_backfill."""
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "CREATED")

        df, changed, backfill, no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed", backfill_qualified_names=None
        )
        assert changed == 1
        assert backfill == 0

    def test_all_unchanged_returns_empty_df(self, tmp_path) -> None:
        td = tmp_path / "transformed" / "table"
        td.mkdir(parents=True)
        _write_table_json(td / "a.json", "a", "db.sch.a", "NO CHANGE")
        _write_table_json(td / "b.json", "b", "db.sch.b", "NO CHANGE")

        df, changed, backfill, no_change = get_tables_needing_column_extraction(
            tmp_path / "transformed"
        )
        assert changed == 0
        assert backfill == 0
        assert no_change == 2
        rows = list(df.iter_rows())
        assert rows == []
