"""Tests for incremental diff generation with deletion detection.

Tests cover:
- create_incremental_diff: Changed tables, columns, schemas, databases
- _detect_deletions: Table-level and column-level deletion detection
- _detect_deleted_columns_for_tables: Cascade column deletion
- _detect_deleted_columns_for_updated_tables: Column diff for updated tables
- _write_metadata: metadata.json generation for Argo routing
"""

import json
import tempfile
from pathlib import Path

from application_sdk.common.incremental.models import IncrementalDiffResult, TableScope
from application_sdk.common.incremental.state.incremental_diff import (
    _detect_deleted_columns_for_tables,
    _detect_deleted_columns_for_updated_tables,
    _detect_deletions,
    _write_metadata,
    create_incremental_diff,
)
from application_sdk.common.incremental.state.table_scope import add_table_to_scope


def _make_table_entity(qualified_name: str, state: str = "CREATED") -> str:
    """Create a single table entity JSON line."""
    return json.dumps(
        {
            "typeName": "Table",
            "status": "ACTIVE",
            "attributes": {"qualifiedName": qualified_name, "name": qualified_name},
            "customAttributes": json.dumps({"incremental_state": state}),
        }
    )


def _make_column_entity(qualified_name: str, table_qualified_name: str) -> str:
    """Create a single column entity JSON line."""
    return json.dumps(
        {
            "typeName": "Column",
            "status": "ACTIVE",
            "attributes": {
                "qualifiedName": qualified_name,
                "name": qualified_name,
                "tableQualifiedName": table_qualified_name,
            },
            "customAttributes": "{}",
        }
    )


def _write_entities(directory: Path, entity_type: str, lines: list[str]) -> None:
    """Write entity JSON lines to a chunk file."""
    entity_dir = directory / entity_type
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "chunk-0.json").write_text("\n".join(lines), encoding="utf-8")


def _make_scope(tables: dict[str, str]) -> TableScope:
    """Create a TableScope from a dict of {qualified_name: state}.

    Uses a plain dict for table_states to avoid requiring rocksdict in tests.
    """
    scope = TableScope(table_states={})
    state_counts: dict[str, int] = {}
    for qn, state in tables.items():
        add_table_to_scope(scope, qn, state)
        state_counts[state] = state_counts.get(state, 0) + 1
    scope.state_counts = state_counts
    return scope


# ---------------------------------------------------------------------------
# create_incremental_diff: basic behavior
# ---------------------------------------------------------------------------


class TestCreateIncrementalDiff:
    """Tests for create_incremental_diff (main orchestrator)."""

    def test_creates_diff_for_changed_tables(self):
        """Changed tables and their columns appear in the diff."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            diff_dir = Path(temp_dir) / "diff"

            _write_entities(
                transformed,
                "table",
                [
                    _make_table_entity("db.schema.t1", "CREATED"),
                    _make_table_entity("db.schema.t2", "NO CHANGE"),
                ],
            )
            _write_entities(
                transformed,
                "column",
                [
                    _make_column_entity("db.schema.t1.col1", "db.schema.t1"),
                    _make_column_entity("db.schema.t2.col2", "db.schema.t2"),
                ],
            )
            _write_entities(
                transformed,
                "schema",
                [
                    json.dumps(
                        {
                            "typeName": "Schema",
                            "attributes": {"qualifiedName": "db.schema"},
                        }
                    )
                ],
            )

            scope = _make_scope(
                {"db.schema.t1": "CREATED", "db.schema.t2": "NO CHANGE"}
            )

            result = create_incremental_diff(
                transformed_dir=transformed,
                incremental_diff_dir=diff_dir,
                table_scope=scope,
            )

            assert result.tables_created == 1
            assert result.columns_total == 1
            assert result.schemas_total == 1
            assert (diff_dir / "table" / "chunk-0.json").exists()
            assert (diff_dir / "column" / "chunk-0.json").exists()
            assert (diff_dir / "metadata.json").exists()

    def test_empty_diff_writes_metadata_with_zero_entities(self):
        """An empty diff still writes metadata.json."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            diff_dir = Path(temp_dir) / "diff"

            _write_entities(
                transformed,
                "table",
                [_make_table_entity("db.schema.t1", "NO CHANGE")],
            )

            scope = _make_scope({"db.schema.t1": "NO CHANGE"})

            result = create_incremental_diff(
                transformed_dir=transformed,
                incremental_diff_dir=diff_dir,
                table_scope=scope,
            )

            assert result.tables_created == 0
            assert result.tables_updated == 0
            assert result.total_changed_entities == 0
            metadata = json.loads(
                (diff_dir / "metadata.json").read_text(encoding="utf-8")
            )
            assert metadata["total_changed_entities"] == 0
            assert metadata["is_incremental"] is True

    def test_deletion_detection_with_previous_state(self):
        """Deleted tables and their columns appear in delete/ directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            previous = Path(temp_dir) / "previous"
            diff_dir = Path(temp_dir) / "diff"

            # Current: only t1
            _write_entities(
                transformed,
                "table",
                [_make_table_entity("db.schema.t1", "NO CHANGE")],
            )

            # Previous: t1 + t2 (t2 will be detected as deleted)
            _write_entities(
                previous,
                "table",
                [
                    _make_table_entity("db.schema.t1", "NO CHANGE"),
                    _make_table_entity("db.schema.t2", "NO CHANGE"),
                ],
            )
            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db.schema.t2.col1", "db.schema.t2"),
                    _make_column_entity("db.schema.t2.col2", "db.schema.t2"),
                ],
            )

            scope = _make_scope({"db.schema.t1": "NO CHANGE"})

            result = create_incremental_diff(
                transformed_dir=transformed,
                incremental_diff_dir=diff_dir,
                table_scope=scope,
                previous_state_dir=previous,
            )

            assert result.tables_deleted == 1
            assert result.columns_deleted == 2
            assert (diff_dir / "delete" / "table" / "chunk-0.json").exists()
            assert (diff_dir / "delete" / "column" / "chunk-0.json").exists()


# ---------------------------------------------------------------------------
# _detect_deletions
# ---------------------------------------------------------------------------


class TestDetectDeletions:
    """Tests for _detect_deletions (table and column deletion detection)."""

    def test_no_previous_state_returns_empty(self):
        """No previous state dir means no deletions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            delete_dir = Path(temp_dir) / "delete"
            counts = _detect_deletions(
                previous_state_dir=Path(temp_dir) / "nonexistent",
                current_table_qns={"t1"},
                updated_table_qns=set(),
                current_column_dir=Path(temp_dir) / "columns",
                delete_dir=delete_dir,
            )
            assert counts["tables_deleted"] == 0
            assert counts["columns_deleted"] == 0

    def test_no_deleted_tables(self):
        """When all previous tables still exist, no deletions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            delete_dir = Path(temp_dir) / "delete"

            _write_entities(
                previous,
                "table",
                [_make_table_entity("db.schema.t1")],
            )

            counts = _detect_deletions(
                previous_state_dir=previous,
                current_table_qns={"db.schema.t1"},
                updated_table_qns=set(),
                current_column_dir=Path(temp_dir) / "columns",
                delete_dir=delete_dir,
            )
            assert counts["tables_deleted"] == 0

    def test_deleted_table_cascade_to_columns(self):
        """Deleted tables cascade to their columns."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            delete_dir = Path(temp_dir) / "delete"

            _write_entities(
                previous,
                "table",
                [
                    _make_table_entity("db.schema.t1"),
                    _make_table_entity("db.schema.t2"),
                ],
            )
            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db.schema.t2.col_a", "db.schema.t2"),
                    _make_column_entity("db.schema.t2.col_b", "db.schema.t2"),
                    _make_column_entity("db.schema.t1.col_x", "db.schema.t1"),
                ],
            )

            counts = _detect_deletions(
                previous_state_dir=previous,
                current_table_qns={"db.schema.t1"},
                updated_table_qns=set(),
                current_column_dir=Path(temp_dir) / "columns",
                delete_dir=delete_dir,
            )

            assert counts["tables_deleted"] == 1
            # Only t2's columns (2) should be cascade-deleted, not t1's
            assert counts["columns_deleted"] == 2

    def test_deleted_columns_for_updated_tables(self):
        """Columns missing from UPDATED tables are detected as deleted."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            delete_dir = Path(temp_dir) / "delete"

            # Previous: t1 had 3 columns
            _write_entities(
                previous,
                "table",
                [_make_table_entity("db.schema.t1")],
            )
            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db.schema.t1.col_a", "db.schema.t1"),
                    _make_column_entity("db.schema.t1.col_b", "db.schema.t1"),
                    _make_column_entity("db.schema.t1.col_c", "db.schema.t1"),
                ],
            )

            # Current: t1 has only 2 columns (col_b removed)
            _write_entities(
                Path(temp_dir) / "wrapper",
                "column",
                [
                    _make_column_entity("db.schema.t1.col_a", "db.schema.t1"),
                    _make_column_entity("db.schema.t1.col_c", "db.schema.t1"),
                ],
            )

            counts = _detect_deletions(
                previous_state_dir=previous,
                current_table_qns={"db.schema.t1"},
                updated_table_qns={"db.schema.t1"},
                current_column_dir=Path(temp_dir) / "wrapper" / "column",
                delete_dir=delete_dir,
            )

            assert counts["tables_deleted"] == 0
            assert counts["columns_deleted"] == 1  # col_b deleted


# ---------------------------------------------------------------------------
# _detect_deleted_columns_for_tables (cascade)
# ---------------------------------------------------------------------------


class TestDetectDeletedColumnsForTables:
    """Tests for cascade column deletion from deleted tables."""

    def test_no_columns_returns_zero(self):
        """No columns in previous state means no cascades."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            previous.mkdir()
            delete_col_dir = Path(temp_dir) / "delete_cols"

            count = _detect_deleted_columns_for_tables(
                previous_state_dir=previous,
                table_qns={"db.schema.t1"},
                delete_column_dir=delete_col_dir,
            )
            assert count == 0

    def test_empty_table_qns_returns_zero(self):
        """Empty table QN set returns zero."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            _write_entities(
                previous,
                "column",
                [_make_column_entity("db.schema.t1.col", "db.schema.t1")],
            )
            delete_col_dir = Path(temp_dir) / "delete_cols"

            count = _detect_deleted_columns_for_tables(
                previous_state_dir=previous,
                table_qns=set(),
                delete_column_dir=delete_col_dir,
            )
            assert count == 0

    def test_cascades_all_columns(self):
        """All columns for deleted tables are written to delete dir."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db.schema.t1.col1", "db.schema.t1"),
                    _make_column_entity("db.schema.t1.col2", "db.schema.t1"),
                    _make_column_entity("db.schema.t2.col3", "db.schema.t2"),
                ],
            )
            delete_col_dir = Path(temp_dir) / "delete_cols"

            count = _detect_deleted_columns_for_tables(
                previous_state_dir=previous,
                table_qns={"db.schema.t1"},
                delete_column_dir=delete_col_dir,
            )
            assert count == 2
            assert (delete_col_dir / "chunk-0.json").exists()


# ---------------------------------------------------------------------------
# _detect_deleted_columns_for_updated_tables
# ---------------------------------------------------------------------------


class TestDetectDeletedColumnsForUpdatedTables:
    """Tests for column-level deletion detection on UPDATED tables."""

    def test_no_missing_columns_returns_zero(self):
        """When all previous columns still exist, returns zero."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            current = Path(temp_dir) / "current"
            delete_col_dir = Path(temp_dir) / "delete_cols"

            _write_entities(
                previous,
                "column",
                [_make_column_entity("db.schema.t1.col1", "db.schema.t1")],
            )
            _write_entities(
                current,
                "column",
                [_make_column_entity("db.schema.t1.col1", "db.schema.t1")],
            )

            count = _detect_deleted_columns_for_updated_tables(
                previous_state_dir=previous,
                current_column_dir=current / "column",
                updated_table_qns={"db.schema.t1"},
                delete_column_dir=delete_col_dir,
            )
            assert count == 0

    def test_detects_missing_columns(self):
        """Columns in previous but not in current are detected as deleted."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            current = Path(temp_dir) / "current"
            delete_col_dir = Path(temp_dir) / "delete_cols"

            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db.schema.t1.col1", "db.schema.t1"),
                    _make_column_entity("db.schema.t1.col2", "db.schema.t1"),
                    _make_column_entity("db.schema.t1.col3", "db.schema.t1"),
                ],
            )
            _write_entities(
                current,
                "column",
                [_make_column_entity("db.schema.t1.col1", "db.schema.t1")],
            )

            count = _detect_deleted_columns_for_updated_tables(
                previous_state_dir=previous,
                current_column_dir=current / "column",
                updated_table_qns={"db.schema.t1"},
                delete_column_dir=delete_col_dir,
            )
            assert count == 2  # col2 and col3 deleted
            assert (delete_col_dir / "chunk-1.json").exists()

    def test_only_checks_updated_tables(self):
        """Columns from non-updated tables are not checked for deletion."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            current = Path(temp_dir) / "current"
            delete_col_dir = Path(temp_dir) / "delete_cols"

            # Previous: t1 had col1, t2 had col2
            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db.schema.t1.col1", "db.schema.t1"),
                    _make_column_entity("db.schema.t2.col2", "db.schema.t2"),
                ],
            )

            # Current: t1 has new_col (col1 was removed), t2 not re-extracted
            _write_entities(
                current,
                "column",
                [_make_column_entity("db.schema.t1.new_col", "db.schema.t1")],
            )

            count = _detect_deleted_columns_for_updated_tables(
                previous_state_dir=previous,
                current_column_dir=current / "column",
                updated_table_qns={"db.schema.t1"},
                delete_column_dir=delete_col_dir,
            )
            # Only t1's col1 should be detected as deleted, not t2's col2
            assert count == 1

    def test_no_updated_tables_returns_zero(self):
        """Empty updated table set returns zero."""
        with tempfile.TemporaryDirectory() as temp_dir:
            previous = Path(temp_dir) / "previous"
            current = Path(temp_dir) / "current"
            delete_col_dir = Path(temp_dir) / "delete_cols"

            _write_entities(
                previous,
                "column",
                [_make_column_entity("db.schema.t1.col1", "db.schema.t1")],
            )
            _write_entities(
                current,
                "column",
                [],
            )

            count = _detect_deleted_columns_for_updated_tables(
                previous_state_dir=previous,
                current_column_dir=current / "column",
                updated_table_qns=set(),
                delete_column_dir=delete_col_dir,
            )
            assert count == 0


# ---------------------------------------------------------------------------
# _write_metadata
# ---------------------------------------------------------------------------


class TestWriteMetadata:
    """Tests for metadata.json generation."""

    def test_writes_all_fields(self):
        """Metadata contains all expected fields."""
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_dir = Path(temp_dir) / "diff"
            diff_dir.mkdir()

            result = IncrementalDiffResult(
                tables_created=5,
                tables_updated=3,
                tables_backfill=1,
                tables_deleted=2,
                columns_total=50,
                columns_deleted=10,
                schemas_total=3,
                databases_total=1,
                total_files=10,
                is_incremental=True,
            )

            _write_metadata(diff_dir, result)

            metadata = json.loads(
                (diff_dir / "metadata.json").read_text(encoding="utf-8")
            )
            assert metadata["is_incremental"] is True
            assert metadata["tables_created"] == 5
            assert metadata["tables_updated"] == 3
            assert metadata["tables_deleted"] == 2
            assert metadata["columns_total"] == 50
            assert metadata["columns_deleted"] == 10
            assert metadata["total_changed_entities"] == 75

    def test_empty_diff_metadata(self):
        """Empty diff produces metadata with zero counts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            diff_dir = Path(temp_dir) / "diff"
            diff_dir.mkdir()

            result = IncrementalDiffResult()
            _write_metadata(diff_dir, result)

            metadata = json.loads(
                (diff_dir / "metadata.json").read_text(encoding="utf-8")
            )
            assert metadata["total_changed_entities"] == 0
            assert metadata["is_incremental"] is True


# ---------------------------------------------------------------------------
# Integration: multiple deletion scenarios
# ---------------------------------------------------------------------------


class TestDeletionIntegration:
    """Integration tests combining table and column deletions."""

    def test_multiple_deleted_tables_with_columns(self):
        """Multiple deleted tables cascade-delete all their columns."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            previous = Path(temp_dir) / "previous"
            diff_dir = Path(temp_dir) / "diff"

            # Current: only t1
            _write_entities(
                transformed,
                "table",
                [_make_table_entity("db.s.t1", "NO CHANGE")],
            )

            # Previous: t1, t2, t3
            _write_entities(
                previous,
                "table",
                [
                    _make_table_entity("db.s.t1"),
                    _make_table_entity("db.s.t2"),
                    _make_table_entity("db.s.t3"),
                ],
            )
            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db.s.t1.c1", "db.s.t1"),
                    _make_column_entity("db.s.t2.c2", "db.s.t2"),
                    _make_column_entity("db.s.t2.c3", "db.s.t2"),
                    _make_column_entity("db.s.t3.c4", "db.s.t3"),
                ],
            )

            scope = _make_scope({"db.s.t1": "NO CHANGE"})

            result = create_incremental_diff(
                transformed_dir=transformed,
                incremental_diff_dir=diff_dir,
                table_scope=scope,
                previous_state_dir=previous,
            )

            assert result.tables_deleted == 2  # t2 and t3
            assert result.columns_deleted == 3  # t2's 2 cols + t3's 1 col

    def test_updated_and_deleted_tables_together(self):
        """Both table deletions and column deletions from updated tables."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            previous = Path(temp_dir) / "previous"
            diff_dir = Path(temp_dir) / "diff"

            # Current: t1 (UPDATED), t2 gone
            _write_entities(
                transformed,
                "table",
                [_make_table_entity("db.s.t1", "UPDATED")],
            )
            _write_entities(
                transformed,
                "column",
                [
                    # t1 lost col_b in this run
                    _make_column_entity("db.s.t1.col_a", "db.s.t1"),
                ],
            )

            # Previous: t1 (with 2 cols), t2 (with 1 col)
            _write_entities(
                previous,
                "table",
                [
                    _make_table_entity("db.s.t1"),
                    _make_table_entity("db.s.t2"),
                ],
            )
            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db.s.t1.col_a", "db.s.t1"),
                    _make_column_entity("db.s.t1.col_b", "db.s.t1"),
                    _make_column_entity("db.s.t2.col_x", "db.s.t2"),
                ],
            )

            scope = _make_scope({"db.s.t1": "UPDATED"})

            result = create_incremental_diff(
                transformed_dir=transformed,
                incremental_diff_dir=diff_dir,
                table_scope=scope,
                previous_state_dir=previous,
            )

            # t2 deleted
            assert result.tables_deleted == 1
            # t2's col_x (cascade) + t1's col_b (column diff) = 2
            assert result.columns_deleted == 2
            # t1 is UPDATED → its current col_a is in the diff
            assert result.columns_total == 1

    def test_no_previous_state_no_deletions(self):
        """First run (no previous state) has no deletions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            diff_dir = Path(temp_dir) / "diff"

            _write_entities(
                transformed,
                "table",
                [_make_table_entity("db.s.t1", "CREATED")],
            )
            _write_entities(
                transformed,
                "column",
                [_make_column_entity("db.s.t1.col1", "db.s.t1")],
            )

            scope = _make_scope({"db.s.t1": "CREATED"})

            result = create_incremental_diff(
                transformed_dir=transformed,
                incremental_diff_dir=diff_dir,
                table_scope=scope,
                previous_state_dir=None,
            )

            assert result.tables_deleted == 0
            assert result.columns_deleted == 0
            assert result.tables_created == 1
