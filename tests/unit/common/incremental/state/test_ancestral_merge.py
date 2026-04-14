"""Tests for ancestral_merge.merge_ancestral_columns and _consolidate_columns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Set
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.common.incremental.models import TableScope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_column_json(directory: Path, filename: str, rows: list[dict]) -> Path:
    """Write a JSONL file (one JSON object per line) into directory."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def _make_column_row(
    type_name: str = "Column",
    qualified_name: str = "default/db/schema/table/col1",
    table_qualified_name: str = "default/db/schema/table",
    status: str = "ACTIVE",
    incremental_state: str = "CREATED",
) -> dict:
    """Create a minimal column row matching the ColumnField schema."""
    return {
        "typeName": type_name,
        "status": status,
        "attributes": {
            "qualifiedName": qualified_name,
            "tableQualifiedName": table_qualified_name,
        },
        "customAttributes": json.dumps({"incremental_state": incremental_state}),
    }


def _make_table_scope(
    table_qns: Set[str], states: dict[str, str] | None = None
) -> TableScope:
    """Create a TableScope with the given tables."""
    actual_states = states or {qn: "NO CHANGE" for qn in table_qns}
    scope = TableScope.model_construct(
        table_qualified_names=table_qns,
        table_states=actual_states,
        tables_with_extracted_columns=set(),
        state_counts={},
    )
    return scope


# ---------------------------------------------------------------------------
# Tests for _consolidate_columns (unit-level, uses real DuckDB)
# ---------------------------------------------------------------------------


class TestConsolidateColumns:
    """Direct tests for _consolidate_columns with real DuckDB."""

    @pytest.fixture
    def duckdb_conn(self):
        """Provide a fresh in-memory DuckDB connection."""
        duckdb = pytest.importorskip("duckdb")
        conn = duckdb.connect(":memory:")
        yield conn
        conn.close()

    def test_empty_inputs_returns_zero_total(self, duckdb_conn, tmp_path):
        """No current or ancestral files yields zero columns."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        new_col_dir = tmp_path / "new" / "column"
        new_col_dir.mkdir(parents=True)

        result = _consolidate_columns(
            conn=duckdb_conn,
            current_json_files=[],
            ancestral_json_files=[],
            new_column_dir=new_col_dir,
            tables_needing_ancestral=set(),
            tables_with_extracted_columns=set(),
            column_chunk_size=100,
        )

        assert result.columns_total == 0
        assert result.columns_from_current == 0
        assert result.columns_from_ancestral == 0

    def test_current_only(self, duckdb_conn, tmp_path):
        """Only current columns, no ancestral state."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        current_dir = tmp_path / "current" / "column"
        new_col_dir = tmp_path / "new" / "column"
        new_col_dir.mkdir(parents=True)

        rows = [
            _make_column_row(
                qualified_name="db/s/t1/c1",
                table_qualified_name="db/s/t1",
            ),
            _make_column_row(
                qualified_name="db/s/t1/c2",
                table_qualified_name="db/s/t1",
            ),
        ]
        _write_column_json(current_dir, "chunk-0-part0.json", rows)

        result = _consolidate_columns(
            conn=duckdb_conn,
            current_json_files=list(current_dir.glob("*.json")),
            ancestral_json_files=[],
            new_column_dir=new_col_dir,
            tables_needing_ancestral=set(),
            tables_with_extracted_columns={"db/s/t1"},
            column_chunk_size=100,
        )

        assert result.columns_from_current == 2
        assert result.columns_from_ancestral == 0
        assert result.columns_total == 2
        # Output file should exist
        output_files = list(new_col_dir.glob("*.json"))
        assert len(output_files) == 1

    def test_ancestral_only(self, duckdb_conn, tmp_path):
        """Only ancestral columns for NO CHANGE tables."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        ancestral_dir = tmp_path / "ancestral" / "column"
        new_col_dir = tmp_path / "new" / "column"
        new_col_dir.mkdir(parents=True)

        rows = [
            _make_column_row(
                qualified_name="db/s/t2/c1",
                table_qualified_name="db/s/t2",
                incremental_state="NO CHANGE",
            ),
        ]
        _write_column_json(ancestral_dir, "chunk-0-part0.json", rows)

        result = _consolidate_columns(
            conn=duckdb_conn,
            current_json_files=[],
            ancestral_json_files=list(ancestral_dir.glob("*.json")),
            new_column_dir=new_col_dir,
            tables_needing_ancestral={"db/s/t2"},
            tables_with_extracted_columns=set(),
            column_chunk_size=100,
        )

        assert result.columns_from_current == 0
        assert result.columns_from_ancestral == 1
        assert result.columns_total == 1

    def test_mixed_current_and_ancestral(self, duckdb_conn, tmp_path):
        """Current columns for CREATED table + ancestral for NO CHANGE table."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        current_dir = tmp_path / "current" / "column"
        ancestral_dir = tmp_path / "ancestral" / "column"
        new_col_dir = tmp_path / "new" / "column"
        new_col_dir.mkdir(parents=True)

        current_rows = [
            _make_column_row(
                qualified_name="db/s/t1/c1",
                table_qualified_name="db/s/t1",
                incremental_state="CREATED",
            ),
        ]
        ancestral_rows = [
            _make_column_row(
                qualified_name="db/s/t2/c1",
                table_qualified_name="db/s/t2",
                incremental_state="NO CHANGE",
            ),
            _make_column_row(
                qualified_name="db/s/t2/c2",
                table_qualified_name="db/s/t2",
                incremental_state="NO CHANGE",
            ),
        ]

        _write_column_json(current_dir, "chunk-0-part0.json", current_rows)
        _write_column_json(ancestral_dir, "chunk-0-part0.json", ancestral_rows)

        result = _consolidate_columns(
            conn=duckdb_conn,
            current_json_files=list(current_dir.glob("*.json")),
            ancestral_json_files=list(ancestral_dir.glob("*.json")),
            new_column_dir=new_col_dir,
            tables_needing_ancestral={"db/s/t2"},
            tables_with_extracted_columns={"db/s/t1"},
            column_chunk_size=100,
        )

        assert result.columns_from_current == 1
        assert result.columns_from_ancestral == 2
        assert result.columns_total == 3

    def test_ancestral_excluded_for_re_extracted_table(self, duckdb_conn, tmp_path):
        """Ancestral columns for a re-extracted table are excluded."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        ancestral_dir = tmp_path / "ancestral" / "column"
        new_col_dir = tmp_path / "new" / "column"
        new_col_dir.mkdir(parents=True)

        rows = [
            _make_column_row(
                qualified_name="db/s/t1/c1",
                table_qualified_name="db/s/t1",
            ),
        ]
        _write_column_json(ancestral_dir, "chunk-0-part0.json", rows)

        # t1 was re-extracted (in tables_with_extracted_columns)
        # so it does NOT need ancestral columns
        result = _consolidate_columns(
            conn=duckdb_conn,
            current_json_files=[],
            ancestral_json_files=list(ancestral_dir.glob("*.json")),
            new_column_dir=new_col_dir,
            tables_needing_ancestral=set(),  # t1 is NOT here
            tables_with_extracted_columns={"db/s/t1"},
            column_chunk_size=100,
        )

        assert result.columns_from_ancestral == 0
        assert result.columns_total == 0

    def test_chunking_produces_multiple_files(self, duckdb_conn, tmp_path):
        """When total columns exceed chunk_size, multiple files are written."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        current_dir = tmp_path / "current" / "column"
        new_col_dir = tmp_path / "new" / "column"
        new_col_dir.mkdir(parents=True)

        rows = [
            _make_column_row(
                qualified_name=f"db/s/t1/c{i}",
                table_qualified_name="db/s/t1",
            )
            for i in range(5)
        ]
        _write_column_json(current_dir, "chunk-0-part0.json", rows)

        result = _consolidate_columns(
            conn=duckdb_conn,
            current_json_files=list(current_dir.glob("*.json")),
            ancestral_json_files=[],
            new_column_dir=new_col_dir,
            tables_needing_ancestral=set(),
            tables_with_extracted_columns={"db/s/t1"},
            column_chunk_size=2,  # Force multiple chunks
        )

        assert result.columns_total == 5
        output_files = list(new_col_dir.glob("chunk-*-part0.json"))
        assert len(output_files) == 3  # ceil(5/2) = 3

    def test_excluded_table_removed_metric(self, duckdb_conn, tmp_path):
        """Ancestral columns for removed tables are counted in excluded_table_removed."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        ancestral_dir = tmp_path / "ancestral" / "column"
        new_col_dir = tmp_path / "new" / "column"
        new_col_dir.mkdir(parents=True)

        rows = [
            _make_column_row(
                qualified_name="db/s/removed_table/c1",
                table_qualified_name="db/s/removed_table",
            ),
        ]
        _write_column_json(ancestral_dir, "chunk-0-part0.json", rows)

        # tables_needing_ancestral does NOT include removed_table
        # tables_with_extracted_columns does NOT include it either
        result = _consolidate_columns(
            conn=duckdb_conn,
            current_json_files=[],
            ancestral_json_files=list(ancestral_dir.glob("*.json")),
            new_column_dir=new_col_dir,
            tables_needing_ancestral={"db/s/other_table"},  # not removed_table
            tables_with_extracted_columns=set(),
            column_chunk_size=100,
        )

        assert result.excluded_table_removed == 1

    def test_excluded_already_extracted_metric(self, duckdb_conn, tmp_path):
        """Ancestral columns for re-extracted tables counted in excluded_already_extracted."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        ancestral_dir = tmp_path / "ancestral" / "column"
        new_col_dir = tmp_path / "new" / "column"
        new_col_dir.mkdir(parents=True)

        # Ancestral has columns for t1 (re-extracted) and t2 (needs ancestral)
        rows = [
            _make_column_row(
                qualified_name="db/s/t1/c1",
                table_qualified_name="db/s/t1",
            ),
            _make_column_row(
                qualified_name="db/s/t1/c2",
                table_qualified_name="db/s/t1",
            ),
            _make_column_row(
                qualified_name="db/s/t2/c1",
                table_qualified_name="db/s/t2",
            ),
        ]
        _write_column_json(ancestral_dir, "chunk-0-part0.json", rows)

        # t2 needs ancestral (so ancestral loading branch executes),
        # but t1 was re-extracted so its ancestral columns are excluded
        result = _consolidate_columns(
            conn=duckdb_conn,
            current_json_files=[],
            ancestral_json_files=list(ancestral_dir.glob("*.json")),
            new_column_dir=new_col_dir,
            tables_needing_ancestral={"db/s/t2"},  # only t2
            tables_with_extracted_columns={"db/s/t1"},  # t1 was re-extracted
            column_chunk_size=100,
        )

        assert result.excluded_already_extracted == 2
        assert result.columns_from_ancestral == 1  # only t2's column

    def test_idempotent_rerun(self, duckdb_conn, tmp_path):
        """Running _consolidate_columns twice with same inputs yields same result."""
        from application_sdk.common.incremental.state.ancestral_merge import (
            _consolidate_columns,
        )

        current_dir = tmp_path / "current" / "column"
        rows = [
            _make_column_row(
                qualified_name="db/s/t1/c1",
                table_qualified_name="db/s/t1",
            ),
        ]
        _write_column_json(current_dir, "chunk-0-part0.json", rows)

        kwargs = dict(
            conn=duckdb_conn,
            current_json_files=list(current_dir.glob("*.json")),
            ancestral_json_files=[],
            tables_needing_ancestral=set(),
            tables_with_extracted_columns={"db/s/t1"},
            column_chunk_size=100,
        )

        new1 = tmp_path / "new1" / "column"
        new1.mkdir(parents=True)
        result1 = _consolidate_columns(new_column_dir=new1, **kwargs)

        new2 = tmp_path / "new2" / "column"
        new2.mkdir(parents=True)
        result2 = _consolidate_columns(new_column_dir=new2, **kwargs)

        assert result1.columns_total == result2.columns_total
        assert result1.columns_from_current == result2.columns_from_current
        assert result1.columns_from_ancestral == result2.columns_from_ancestral


# ---------------------------------------------------------------------------
# Tests for merge_ancestral_columns (integration-level with mocks)
# ---------------------------------------------------------------------------


class TestMergeAncestralColumns:
    """Tests for the top-level merge_ancestral_columns function."""

    def test_no_current_no_ancestral(self, tmp_path):
        """No column files at all yields zero merge result."""
        duckdb = pytest.importorskip("duckdb")
        conn = duckdb.connect(":memory:")

        from application_sdk.common.incremental.state.ancestral_merge import (
            merge_ancestral_columns,
        )

        current = tmp_path / "current"
        current.mkdir()
        new_state = tmp_path / "new_state"
        new_state.mkdir()

        scope = _make_table_scope({"db/s/t1"})

        result, tables_with_cols = merge_ancestral_columns(
            current_transformed_dir=current,
            previous_state_dir=None,
            new_state_dir=new_state,
            table_scope=scope,
            column_chunk_size=100,
            conn=conn,
        )

        assert result.columns_total == 0
        assert tables_with_cols == set()
        conn.close()

    def test_raises_on_unparseable_columns(self, tmp_path):
        """Raises RuntimeError when get_table_qns_from_columns returns None."""
        duckdb = pytest.importorskip("duckdb")
        conn = duckdb.connect(":memory:")

        from application_sdk.common.incremental.state.ancestral_merge import (
            merge_ancestral_columns,
        )

        current = tmp_path / "current"
        col_dir = current / "column"
        col_dir.mkdir(parents=True)
        # Write a JSON file that exists but has no valid table references
        (col_dir / "bad.json").write_text("{}")

        new_state = tmp_path / "new_state"
        new_state.mkdir()

        scope = _make_table_scope({"db/s/t1"})

        mock_fn = MagicMock(return_value=None)
        mock_fn.__name__ = "get_table_qns_from_columns"
        with patch(
            "application_sdk.common.incremental.state.ancestral_merge.get_table_qns_from_columns",
            mock_fn,
        ):
            with pytest.raises(RuntimeError, match="Failed to determine tables"):
                merge_ancestral_columns(
                    current_transformed_dir=current,
                    previous_state_dir=None,
                    new_state_dir=new_state,
                    table_scope=scope,
                    column_chunk_size=100,
                    conn=conn,
                )

        conn.close()
