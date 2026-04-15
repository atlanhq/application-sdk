"""Tests for incremental diff generation with deletion detection.

Tests cover:
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
    return json.dumps(
        {
            "typeName": "Table",
            "status": "ACTIVE",
            "attributes": {"qualifiedName": qualified_name, "name": qualified_name},
            "customAttributes": json.dumps({"incremental_state": state}),
        }
    )


def _make_column_entity(qualified_name: str, table_qualified_name: str) -> str:
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
    entity_dir = directory / entity_type
    entity_dir.mkdir(parents=True, exist_ok=True)
    (entity_dir / "chunk-0.json").write_text("\n".join(lines), encoding="utf-8")


def _make_scope(tables: dict[str, str]) -> TableScope:
    scope = TableScope(table_states={})
    state_counts: dict[str, int] = {}
    for qn, state in tables.items():
        add_table_to_scope(scope, qn, state)
        state_counts[state] = state_counts.get(state, 0) + 1
    scope.state_counts = state_counts
    return scope


# ---------------------------------------------------------------------------
# _detect_deletions
# ---------------------------------------------------------------------------


class TestDetectDeletions:
    def test_detects_deleted_tables(self):
        """Tables in previous but not in current → deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            delete_dir = Path(tmp) / "delete"
            current_col_dir = Path(tmp) / "current" / "column"

            _write_entities(
                prev,
                "table",
                [
                    _make_table_entity("db/schema/t1"),
                    _make_table_entity("db/schema/t2"),
                    _make_table_entity("db/schema/t3"),
                ],
            )

            counts = _detect_deletions(
                previous_state_dir=prev,
                current_table_qns={"db/schema/t1", "db/schema/t2"},
                updated_table_qns=set(),
                current_column_dir=current_col_dir,
                delete_dir=delete_dir,
            )

            assert counts["tables_deleted"] == 1
            delete_table_dir = delete_dir / "table"
            assert delete_table_dir.exists()

    def test_no_deletions_when_all_present(self):
        """All previous tables in current → no deletions."""
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            delete_dir = Path(tmp) / "delete"
            current_col_dir = Path(tmp) / "current" / "column"

            _write_entities(
                prev, "table", [_make_table_entity("db/schema/t1")]
            )

            counts = _detect_deletions(
                previous_state_dir=prev,
                current_table_qns={"db/schema/t1"},
                updated_table_qns=set(),
                current_column_dir=current_col_dir,
                delete_dir=delete_dir,
            )

            assert counts["tables_deleted"] == 0
            assert counts["columns_deleted"] == 0

    def test_no_previous_tables(self):
        """No previous table directory → no deletions."""
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            prev.mkdir()
            delete_dir = Path(tmp) / "delete"
            current_col_dir = Path(tmp) / "current" / "column"

            counts = _detect_deletions(
                previous_state_dir=prev,
                current_table_qns={"db/schema/t1"},
                updated_table_qns=set(),
                current_column_dir=current_col_dir,
                delete_dir=delete_dir,
            )

            assert counts["tables_deleted"] == 0
            assert counts["columns_deleted"] == 0

    def test_cascade_deletes_columns(self):
        """Deleted table → all its columns are cascade-deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            delete_dir = Path(tmp) / "delete"
            current_col_dir = Path(tmp) / "current" / "column"

            _write_entities(
                prev, "table", [_make_table_entity("db/schema/t1")]
            )
            _write_entities(
                prev,
                "column",
                [
                    _make_column_entity("db/schema/t1/col1", "db/schema/t1"),
                    _make_column_entity("db/schema/t1/col2", "db/schema/t1"),
                ],
            )

            counts = _detect_deletions(
                previous_state_dir=prev,
                current_table_qns=set(),
                updated_table_qns=set(),
                current_column_dir=current_col_dir,
                delete_dir=delete_dir,
            )

            assert counts["tables_deleted"] == 1
            assert counts["columns_deleted"] == 2


# ---------------------------------------------------------------------------
# _detect_deleted_columns_for_tables (cascade)
# ---------------------------------------------------------------------------


class TestDetectDeletedColumnsForTables:
    def test_cascade_columns_for_deleted_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            delete_col_dir = Path(tmp) / "delete" / "column"

            _write_entities(
                prev,
                "column",
                [
                    _make_column_entity("db/s/t1/c1", "db/s/t1"),
                    _make_column_entity("db/s/t1/c2", "db/s/t1"),
                    _make_column_entity("db/s/t2/c1", "db/s/t2"),
                ],
            )

            count = _detect_deleted_columns_for_tables(
                previous_state_dir=prev,
                table_qns={"db/s/t1"},
                delete_column_dir=delete_col_dir,
            )

            assert count == 2

    def test_no_columns_for_deleted_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            delete_col_dir = Path(tmp) / "delete" / "column"

            _write_entities(
                prev,
                "column",
                [_make_column_entity("db/s/t2/c1", "db/s/t2")],
            )

            count = _detect_deleted_columns_for_tables(
                previous_state_dir=prev,
                table_qns={"db/s/t1"},
                delete_column_dir=delete_col_dir,
            )

            assert count == 0

    def test_empty_table_qns(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            delete_col_dir = Path(tmp) / "delete" / "column"

            count = _detect_deleted_columns_for_tables(
                previous_state_dir=prev,
                table_qns=set(),
                delete_column_dir=delete_col_dir,
            )

            assert count == 0


# ---------------------------------------------------------------------------
# _detect_deleted_columns_for_updated_tables
# ---------------------------------------------------------------------------


class TestDetectDeletedColumnsForUpdatedTables:
    def test_detects_removed_columns(self):
        """Column in previous but not in current for UPDATED table → deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            current_col_dir = Path(tmp) / "current" / "column"
            delete_col_dir = Path(tmp) / "delete" / "column"

            _write_entities(
                prev,
                "column",
                [
                    _make_column_entity("db/s/t1/c1", "db/s/t1"),
                    _make_column_entity("db/s/t1/c2", "db/s/t1"),
                    _make_column_entity("db/s/t1/c3", "db/s/t1"),
                ],
            )
            _write_entities(
                Path(tmp) / "current",
                "column",
                [
                    _make_column_entity("db/s/t1/c1", "db/s/t1"),
                    _make_column_entity("db/s/t1/c3", "db/s/t1"),
                ],
            )

            count = _detect_deleted_columns_for_updated_tables(
                previous_state_dir=prev,
                current_column_dir=current_col_dir,
                updated_table_qns={"db/s/t1"},
                delete_column_dir=delete_col_dir,
            )

            assert count == 1  # c2 was deleted

    def test_no_deleted_columns(self):
        """All previous columns still present → no deletes."""
        with tempfile.TemporaryDirectory() as tmp:
            prev = Path(tmp) / "previous"
            current_col_dir = Path(tmp) / "current" / "column"
            delete_col_dir = Path(tmp) / "delete" / "column"

            _write_entities(
                prev,
                "column",
                [_make_column_entity("db/s/t1/c1", "db/s/t1")],
            )
            _write_entities(
                Path(tmp) / "current",
                "column",
                [_make_column_entity("db/s/t1/c1", "db/s/t1")],
            )

            count = _detect_deleted_columns_for_updated_tables(
                previous_state_dir=prev,
                current_column_dir=current_col_dir,
                updated_table_qns={"db/s/t1"},
                delete_column_dir=delete_col_dir,
            )

            assert count == 0

    def test_empty_updated_tables(self):
        count = _detect_deleted_columns_for_updated_tables(
            previous_state_dir=Path("/nonexistent"),
            current_column_dir=Path("/nonexistent"),
            updated_table_qns=set(),
            delete_column_dir=Path("/nonexistent"),
        )
        assert count == 0


# ---------------------------------------------------------------------------
# _write_metadata
# ---------------------------------------------------------------------------


class TestWriteMetadata:
    def test_writes_metadata_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            diff_dir = Path(tmp)
            result = IncrementalDiffResult(
                tables_created=5,
                tables_updated=3,
                tables_backfill=1,
                tables_deleted=2,
                columns_total=100,
                columns_deleted=10,
                schemas_total=2,
                databases_total=1,
                total_files=15,
                is_incremental=True,
            )

            _write_metadata(diff_dir, result)

            meta_path = diff_dir / "metadata.json"
            assert meta_path.exists()

            meta = json.loads(meta_path.read_text())
            assert meta["is_incremental"] is True
            assert meta["tables_created"] == 5
            assert meta["tables_deleted"] == 2
            assert meta["columns_deleted"] == 10
            assert meta["total_changed_entities"] == result.total_changed_entities

    def test_metadata_total_changed_entities(self):
        result = IncrementalDiffResult(
            tables_created=1,
            tables_updated=2,
            tables_backfill=3,
            tables_deleted=4,
            columns_total=10,
            columns_deleted=5,
            schemas_total=1,
            databases_total=1,
        )
        assert result.total_changed_entities == 27


# ---------------------------------------------------------------------------
# create_incremental_diff (integration)
# ---------------------------------------------------------------------------


class TestCreateIncrementalDiffWithDeletions:
    def test_diff_with_deletions(self):
        """Full flow: changed tables + deleted tables + cascade columns."""
        with tempfile.TemporaryDirectory() as tmp:
            transformed = Path(tmp) / "transformed"
            previous = Path(tmp) / "previous"
            diff_dir = Path(tmp) / "diff"

            # Current run has t1 (CREATED) and t2 (UPDATED)
            _write_entities(
                transformed,
                "table",
                [
                    _make_table_entity("db/s/t1", "CREATED"),
                    _make_table_entity("db/s/t2", "UPDATED"),
                ],
            )
            _write_entities(
                transformed,
                "column",
                [
                    _make_column_entity("db/s/t1/c1", "db/s/t1"),
                    _make_column_entity("db/s/t2/c1", "db/s/t2"),
                ],
            )

            # Previous state had t1, t2, and t3
            _write_entities(
                previous,
                "table",
                [
                    _make_table_entity("db/s/t1"),
                    _make_table_entity("db/s/t2"),
                    _make_table_entity("db/s/t3"),
                ],
            )
            _write_entities(
                previous,
                "column",
                [
                    _make_column_entity("db/s/t3/c1", "db/s/t3"),
                    _make_column_entity("db/s/t3/c2", "db/s/t3"),
                ],
            )

            scope = _make_scope(
                {"db/s/t1": "CREATED", "db/s/t2": "UPDATED"}
            )

            result = create_incremental_diff(
                transformed_dir=transformed,
                incremental_diff_dir=diff_dir,
                table_scope=scope,
                previous_state_dir=previous,
            )

            assert result.tables_created == 1
            assert result.tables_updated == 1
            assert result.tables_deleted == 1  # t3 deleted
            assert result.columns_deleted == 2  # t3's 2 columns cascade-deleted
            assert result.is_incremental is True

            # metadata.json should exist
            meta = json.loads((diff_dir / "metadata.json").read_text())
            assert meta["tables_deleted"] == 1
            assert meta["columns_deleted"] == 2

    def test_first_run_no_deletions(self):
        """First run (no previous state) → no deletion detection."""
        with tempfile.TemporaryDirectory() as tmp:
            transformed = Path(tmp) / "transformed"
            diff_dir = Path(tmp) / "diff"

            _write_entities(
                transformed,
                "table",
                [_make_table_entity("db/s/t1", "CREATED")],
            )

            scope = _make_scope({"db/s/t1": "CREATED"})

            result = create_incremental_diff(
                transformed_dir=transformed,
                incremental_diff_dir=diff_dir,
                table_scope=scope,
                previous_state_dir=None,
            )

            assert result.tables_deleted == 0
            assert result.columns_deleted == 0
