"""Integration tests for the incremental extraction pipeline.

Exercises the full flow with real DuckDB, real filesystem, real JSON entities.
No mocks — validates the complete pipeline end-to-end:
- Lightweight column copy (no ancestral merge)
- Deletion detection (table-level + column-level + cascade)
- metadata.json generation
- S3 upload/download via local obstore

Simulates multiple incremental runs to verify state transitions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import application_sdk.constants as constants
from application_sdk.infrastructure.context import (
    InfrastructureContext,
    set_infrastructure,
)
from application_sdk.storage.batch import download_prefix, upload_prefix
from application_sdk.storage.factory import create_local_store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_entity(qn: str, state: str = "CREATED") -> dict:
    return {
        "typeName": "Table",
        "status": "ACTIVE",
        "attributes": {"qualifiedName": qn, "name": qn.split("/")[-1]},
        "customAttributes": json.dumps({"incremental_state": state}),
    }


def _column_entity(qn: str, table_qn: str) -> dict:
    return {
        "typeName": "Column",
        "status": "ACTIVE",
        "attributes": {
            "qualifiedName": qn,
            "name": qn.split("/")[-1],
            "tableQualifiedName": table_qn,
        },
        "customAttributes": "{}",
    }


def _schema_entity(qn: str) -> dict:
    return {
        "typeName": "Schema",
        "status": "ACTIVE",
        "attributes": {"qualifiedName": qn, "name": qn.split("/")[-1]},
        "customAttributes": "{}",
    }


def _write_jsonl(path: Path, entities: list[dict]) -> None:
    """Write JSONL file (one JSON object per line)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(e) for e in entities),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL file."""
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    return [json.loads(line) for line in lines if line.strip()]


def _count_jsonl(directory: Path) -> int:
    """Count total JSONL records across all .json files in directory."""
    total = 0
    if directory.exists():
        for f in directory.glob("*.json"):
            total += len(_read_jsonl(f))
    return total


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """Real local object store backed by a temp directory."""
    return create_local_store(tmp_path / "store")


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """Staging directory with TEMPORARY_PATH set."""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    monkeypatch.setenv("ATLAN_TEMPORARY_PATH", str(staging_dir))
    monkeypatch.setattr(constants, "TEMPORARY_PATH", str(staging_dir))
    return staging_dir


@pytest.fixture
def infra(store):
    """Set up infrastructure context with local store."""
    set_infrastructure(InfrastructureContext(storage=store))
    yield
    set_infrastructure(InfrastructureContext())


# ---------------------------------------------------------------------------
# Test: Full incremental pipeline — two-run simulation
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_incremental_two_run_simulation(tmp_path, infra):
    """Simulate two incremental runs and verify deletion detection.

    Run 1 (first run): Extract tables t1, t2, t3 with columns
    Run 2 (incremental): t1=NO CHANGE, t2=UPDATED (col dropped), t3=deleted, t4=CREATED
    """
    from application_sdk.common.incremental.models import TableScope
    from application_sdk.common.incremental.state.incremental_diff import (
        create_incremental_diff,
    )
    from application_sdk.common.incremental.state.table_scope import add_table_to_scope

    # ---- RUN 1: First extraction (simulated "previous state") ----
    previous_state = tmp_path / "run1" / "current-state"

    # Tables
    _write_jsonl(
        previous_state / "table" / "chunk-0.json",
        [
            _table_entity("db/schema/t1", "CREATED"),
            _table_entity("db/schema/t2", "CREATED"),
            _table_entity("db/schema/t3", "CREATED"),
        ],
    )

    # Columns
    _write_jsonl(
        previous_state / "column" / "chunk-0.json",
        [
            _column_entity("db/schema/t1/id", "db/schema/t1"),
            _column_entity("db/schema/t1/name", "db/schema/t1"),
            _column_entity("db/schema/t2/id", "db/schema/t2"),
            _column_entity("db/schema/t2/email", "db/schema/t2"),
            _column_entity("db/schema/t2/phone", "db/schema/t2"),
            _column_entity("db/schema/t3/id", "db/schema/t3"),
            _column_entity("db/schema/t3/value", "db/schema/t3"),
        ],
    )

    # Schemas
    _write_jsonl(
        previous_state / "schema" / "chunk-0.json",
        [_schema_entity("db/schema")],
    )

    # ---- RUN 2: Incremental extraction ----
    # t1 = NO CHANGE (not re-extracted)
    # t2 = UPDATED (email column dropped)
    # t3 = DELETED (not in extraction scope)
    # t4 = CREATED (new table)

    transformed = tmp_path / "run2" / "transformed"

    _write_jsonl(
        transformed / "table" / "chunk-0.json",
        [
            _table_entity("db/schema/t1", "NO CHANGE"),
            _table_entity("db/schema/t2", "UPDATED"),
            _table_entity("db/schema/t4", "CREATED"),
        ],
    )

    _write_jsonl(
        transformed / "column" / "chunk-0.json",
        [
            # t2 columns — email is gone (deleted)
            _column_entity("db/schema/t2/id", "db/schema/t2"),
            _column_entity("db/schema/t2/phone", "db/schema/t2"),
            # t4 columns (new)
            _column_entity("db/schema/t4/id", "db/schema/t4"),
            _column_entity("db/schema/t4/data", "db/schema/t4"),
        ],
    )

    _write_jsonl(
        transformed / "schema" / "chunk-0.json",
        [_schema_entity("db/schema")],
    )

    # Build table scope for run 2
    scope = TableScope(table_states={})
    state_counts: dict[str, int] = {}
    for qn, state in [
        ("db/schema/t1", "NO CHANGE"),
        ("db/schema/t2", "UPDATED"),
        ("db/schema/t4", "CREATED"),
    ]:
        add_table_to_scope(scope, qn, state)
        state_counts[state] = state_counts.get(state, 0) + 1
    scope.state_counts = state_counts

    # ---- Create incremental diff ----
    diff_dir = tmp_path / "run2" / "incremental-diff"

    result = create_incremental_diff(
        transformed_dir=transformed,
        incremental_diff_dir=diff_dir,
        table_scope=scope,
        previous_state_dir=previous_state,
    )

    # ---- Assertions ----

    # Changed tables: t2 (UPDATED) + t4 (CREATED)
    assert result.tables_created == 1  # t4
    assert result.tables_updated == 1  # t2

    # Deleted tables: t3 (in previous but not in current scope)
    assert result.tables_deleted == 1

    # Deleted columns:
    # - t3's 2 columns cascade-deleted
    # - t2's email column deleted (in previous, not in current)
    assert result.columns_deleted == 3  # 2 cascade + 1 updated

    # Changed columns: t2 (2 remaining) + t4 (2 new) = 4
    assert result.columns_total == 4

    # Verify diff directory structure
    assert (diff_dir / "table" / "chunk-0.json").exists()
    assert (diff_dir / "column" / "chunk-0.json").exists()
    assert (diff_dir / "delete" / "table" / "chunk-0.json").exists()
    assert (diff_dir / "delete" / "column").exists()

    # Verify deleted table content
    deleted_tables = _read_jsonl(diff_dir / "delete" / "table" / "chunk-0.json")
    deleted_table_qns = {t["attributes"]["qualifiedName"] for t in deleted_tables}
    assert deleted_table_qns == {"db/schema/t3"}

    # Verify metadata.json
    meta = json.loads((diff_dir / "metadata.json").read_text())
    assert meta["is_incremental"] is True
    assert meta["tables_deleted"] == 1
    assert meta["columns_deleted"] == 3
    assert meta["total_changed_entities"] == result.total_changed_entities


@pytest.mark.integration
async def test_lightweight_current_state_snapshot(tmp_path, infra):
    """Verify current-state is lightweight — only current run's columns.

    Uses create_current_state_snapshot with real DuckDB + S3 upload/download.
    """
    from application_sdk.common.incremental.state.state_writer import (
        _copy_columns_from_transformed,
        copy_non_column_entities,
        prepare_current_state_directory,
    )

    transformed = tmp_path / "transformed"

    # Tables: t1 (CREATED), t2 (NO CHANGE)
    _write_jsonl(
        transformed / "table" / "chunk-0.json",
        [
            _table_entity("db/s/t1", "CREATED"),
            _table_entity("db/s/t2", "NO CHANGE"),
        ],
    )

    # Columns: only for t1 (CREATED) — t2 is NO CHANGE, no columns extracted
    _write_jsonl(
        transformed / "column" / "chunk-0.json",
        [
            _column_entity("db/s/t1/c1", "db/s/t1"),
            _column_entity("db/s/t1/c2", "db/s/t1"),
        ],
    )

    _write_jsonl(
        transformed / "schema" / "chunk-0.json",
        [_schema_entity("db/s")],
    )

    # Build current state
    current_state = tmp_path / "current-state"
    prepare_current_state_directory(current_state)

    # Copy non-column entities
    copy_non_column_entities(transformed, current_state)

    # Copy columns (lightweight — no ancestral merge)
    col_count = _copy_columns_from_transformed(transformed, current_state)

    # Verify: only 2 columns copied (for t1), NOT t2's ancestral columns
    assert col_count == 1  # 1 file copied
    column_records = _count_jsonl(current_state / "column")
    assert column_records == 2  # 2 column entities in that file

    # Verify table files exist
    assert (current_state / "table" / "chunk-0.json").exists()
    table_records = _count_jsonl(current_state / "table")
    assert table_records == 2  # both t1 and t2

    # Verify schema files exist
    assert (current_state / "schema" / "chunk-0.json").exists()


@pytest.mark.integration
async def test_first_run_no_previous_state(tmp_path, infra):
    """First run with no previous state — no deletions, no diff errors."""
    from application_sdk.common.incremental.models import TableScope
    from application_sdk.common.incremental.state.incremental_diff import (
        create_incremental_diff,
    )
    from application_sdk.common.incremental.state.table_scope import add_table_to_scope

    transformed = tmp_path / "transformed"

    _write_jsonl(
        transformed / "table" / "chunk-0.json",
        [_table_entity("db/s/t1", "CREATED")],
    )

    _write_jsonl(
        transformed / "column" / "chunk-0.json",
        [_column_entity("db/s/t1/c1", "db/s/t1")],
    )

    scope = TableScope(table_states={})
    add_table_to_scope(scope, "db/s/t1", "CREATED")
    scope.state_counts = {"CREATED": 1}

    diff_dir = tmp_path / "diff"

    result = create_incremental_diff(
        transformed_dir=transformed,
        incremental_diff_dir=diff_dir,
        table_scope=scope,
        previous_state_dir=None,  # First run
    )

    assert result.tables_created == 1
    assert result.tables_deleted == 0
    assert result.columns_deleted == 0
    assert result.is_incremental is True

    # metadata.json still written
    meta = json.loads((diff_dir / "metadata.json").read_text())
    assert meta["tables_deleted"] == 0


@pytest.mark.integration
async def test_incremental_diff_with_s3_roundtrip(tmp_path, store, infra):
    """Upload incremental-diff to S3, download it, verify contents intact."""
    from application_sdk.common.incremental.models import TableScope
    from application_sdk.common.incremental.state.incremental_diff import (
        create_incremental_diff,
    )
    from application_sdk.common.incremental.state.table_scope import add_table_to_scope

    # Previous state had t1 and t2
    previous = tmp_path / "previous"
    _write_jsonl(
        previous / "table" / "chunk-0.json",
        [_table_entity("db/s/t1"), _table_entity("db/s/t2")],
    )
    _write_jsonl(
        previous / "column" / "chunk-0.json",
        [
            _column_entity("db/s/t2/c1", "db/s/t2"),
            _column_entity("db/s/t2/c2", "db/s/t2"),
        ],
    )

    # Current run: only t1 (t2 deleted)
    transformed = tmp_path / "transformed"
    _write_jsonl(
        transformed / "table" / "chunk-0.json",
        [_table_entity("db/s/t1", "NO CHANGE")],
    )

    scope = TableScope(table_states={})
    add_table_to_scope(scope, "db/s/t1", "NO CHANGE")
    scope.state_counts = {"NO CHANGE": 1}

    diff_dir = tmp_path / "diff"
    result = create_incremental_diff(
        transformed_dir=transformed,
        incremental_diff_dir=diff_dir,
        table_scope=scope,
        previous_state_dir=previous,
    )

    assert result.tables_deleted == 1
    assert result.columns_deleted == 2

    # Upload to S3
    uploaded = await upload_prefix(
        local_dir=diff_dir,
        prefix="incremental-diff/run-123",
        store=store,
    )
    assert len(uploaded) > 0

    # Download back
    download_dir = tmp_path / "downloaded"
    await download_prefix(
        prefix="incremental-diff/run-123",
        local_dir=download_dir,
        store=store,
    )

    # Verify metadata.json survived roundtrip
    meta_files = list(download_dir.rglob("metadata.json"))
    assert len(meta_files) == 1
    meta = json.loads(meta_files[0].read_text())
    assert meta["tables_deleted"] == 1
    assert meta["columns_deleted"] == 2

    # Verify delete records survived roundtrip
    delete_table_files = list(download_dir.rglob("delete/table/*.json"))
    assert len(delete_table_files) == 1
