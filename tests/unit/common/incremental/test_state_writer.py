"""Tests for current state writer utilities.

Tests cover public functions with real business logic:
- copy_non_column_entities: Entity iteration and parallel copy
- cleanup_previous_state: Safe cleanup with exception handling
- prepare_current_state_directory: Idempotent directory cleanup
- prepare_previous_state: Conditional materialize (deprecated shim)
- download_transformed_data: Output path validation
- _copy_columns_from_transformed: Lightweight column copy
- upload_current_state: Single-call upload with S3 prefix derivation
- create_current_state_snapshot: First-run + incremental orchestration
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.common.incremental.models import TableScope
from application_sdk.common.incremental.state.state_writer import (
    CurrentStateResult,
    _copy_columns_from_transformed,
    _reset_directory,
    cleanup_previous_state,
    copy_non_column_entities,
    create_current_state_snapshot,
    download_transformed_data,
    prepare_current_state_directory,
    prepare_previous_state,
    upload_current_state,
)
from application_sdk.common.incremental.state.store import CurrentStateStore
from application_sdk.common.incremental.state.table_scope import add_table_to_scope
from application_sdk.storage.errors import StorageError
from application_sdk.storage.ops import _put

# ---------------------------------------------------------------------------
# copy_non_column_entities
# ---------------------------------------------------------------------------


class TestCopyNonColumnEntities:
    """Tests for copy_non_column_entities (entity iteration and parallel copy)."""

    def test_copies_table_schema_database(self):
        """Copies table, schema, and database entity files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"

            # Create entity directories with files
            for entity in ["table", "schema", "database"]:
                entity_dir = transformed / entity
                entity_dir.mkdir(parents=True)
                (entity_dir / "chunk-0.json").write_text("{}")

            counts = copy_non_column_entities(transformed, state)

            assert counts["table"] == 1
            assert counts["schema"] == 1
            assert counts["database"] == 1
            assert (state / "table" / "chunk-0.json").exists()
            assert (state / "schema" / "chunk-0.json").exists()
            assert (state / "database" / "chunk-0.json").exists()

    def test_skips_missing_entity_dirs(self):
        """Skips entity types that don't have directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"

            # Only create table directory (no schema/database)
            table_dir = transformed / "table"
            table_dir.mkdir(parents=True)
            (table_dir / "chunk-0.json").write_text("{}")

            counts = copy_non_column_entities(transformed, state)

            assert counts.get("table") == 1
            assert "schema" not in counts
            assert "database" not in counts

    def test_does_not_copy_columns(self):
        """Column directory is NOT copied (handled by merge)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"

            # Create column directory
            col_dir = transformed / "column"
            col_dir.mkdir(parents=True)
            (col_dir / "chunk-0.json").write_text("{}")

            counts = copy_non_column_entities(transformed, state)

            assert "column" not in counts
            assert not (state / "column").exists()


# ---------------------------------------------------------------------------
# cleanup_previous_state
# ---------------------------------------------------------------------------


class TestCleanupPreviousState:
    """Tests for cleanup_previous_state (safe cleanup)."""

    def test_removes_existing_directory(self):
        """Removes directory when it exists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            prev_dir = Path(temp_dir) / "previous"
            prev_dir.mkdir()
            (prev_dir / "data.json").write_text("{}")

            cleanup_previous_state(prev_dir)

            assert not prev_dir.exists()

    def test_none_is_noop(self):
        """Passing None does nothing (no exception)."""
        cleanup_previous_state(None)

    def test_nonexistent_dir_is_noop(self):
        """Passing a nonexistent path does nothing."""
        cleanup_previous_state(Path("/nonexistent/dir"))

    def test_rmtree_failure_does_not_raise(self):
        """Cleanup failure is logged but does NOT raise (non-critical)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            prev_dir = Path(temp_dir) / "previous"
            prev_dir.mkdir()

            with patch("shutil.rmtree", side_effect=PermissionError("denied")):
                # Should not raise
                cleanup_previous_state(prev_dir)


# ---------------------------------------------------------------------------
# prepare_current_state_directory
# ---------------------------------------------------------------------------


class TestPrepareCurrentStateDirectory:
    """Tests for prepare_current_state_directory (idempotent cleanup)."""

    def test_creates_fresh_directory(self):
        """Creates directory when it doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "current-state"

            prepare_current_state_directory(state_dir)

            assert state_dir.exists()

    def test_clears_existing_directory(self):
        """Removes existing content and recreates an empty directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "current-state"
            state_dir.mkdir()
            (state_dir / "old-data.json").write_text("{}")

            prepare_current_state_directory(state_dir)

            assert state_dir.exists()
            assert list(state_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# prepare_previous_state
# ---------------------------------------------------------------------------


class TestPreparePreviousState:
    """Tests for prepare_previous_state (conditional download)."""

    async def test_returns_none_when_not_available(self):
        """Returns None when current_state_available is False."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = await prepare_previous_state(
                connection_qualified_name="t/c/123",
                current_state_available=False,
                current_state_dir=Path(temp_dir) / "state",
                application_name="oracle",
            )

        assert result is None

    async def test_downloads_previous_state(self, memory_store, tmp_path):
        """Materializes beside the given directory, as the old layout did."""
        state_dir = tmp_path / "current-state"
        state_dir.mkdir()

        with pytest.warns(DeprecationWarning, match="materialize_previous_state"):
            result = await prepare_previous_state(
                connection_qualified_name="t/c/123",
                current_state_available=True,
                current_state_dir=state_dir,
                application_name="oracle",
            )

        assert result == tmp_path / "current-state.previous"
        assert result.exists()

    async def test_download_failure_raises_state_download_error(
        self, memory_store, tmp_path
    ):
        with patch(
            "application_sdk.common.incremental.state.store.list_data_objects",
            new=AsyncMock(side_effect=StorageError("S3 failure")),
        ):
            with pytest.raises(
                Exception, match="Failed to download previous state"
            ) as exc_info:
                await prepare_previous_state(
                    connection_qualified_name="t/c/123",
                    current_state_available=True,
                    current_state_dir=tmp_path / "current-state",
                    application_name="oracle",
                )
        assert "S3 failure" in str(exc_info.value.__cause__)

    async def test_previous_state_lands_unnested_in_temp_dir(
        self, memory_store, tmp_path
    ) -> None:
        """Previous state must land at ``<temp>/<entity>/`` for the diff reader.

        Real store, real bytes: create_incremental_diff globs
        ``<previous_state_dir>/table/*.json``, so a prefix repeated inside the
        temp dir silently reports "no previous tables" (FND-340).
        """
        prefix = "persistent-artifacts/apps/oracle/connection/123/current-state"
        await _put(
            f"{prefix}/table/chunk-0.json", b'{"t": 1}', memory_store, normalize=False
        )

        state_dir = tmp_path / "current-state"
        state_dir.mkdir()

        previous = await prepare_previous_state(
            connection_qualified_name="default/oracle/123",
            current_state_available=True,
            current_state_dir=state_dir,
            application_name="oracle",
        )

        assert previous is not None
        assert (previous / "table" / "chunk-0.json").read_bytes() == b'{"t": 1}'
        assert not (previous / "persistent-artifacts").exists()

    async def test_stale_local_files_are_pruned(self, memory_store, tmp_path):
        """A leftover from an earlier download is removed by the sync, not
        trusted as part of the previous state."""
        prefix = "persistent-artifacts/apps/oracle/connection/123/current-state"
        await _put(f"{prefix}/table/chunk-0.json", b'{"t": 1}', memory_store)
        state_dir = tmp_path / "current-state"
        stale = tmp_path / "current-state.previous" / "table" / "leftover.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("{}")

        previous = await prepare_previous_state(
            connection_qualified_name="default/oracle/123",
            current_state_available=True,
            current_state_dir=state_dir,
            application_name="oracle",
        )

        assert previous is not None
        assert not stale.exists()
        assert (previous / "table" / "chunk-0.json").exists()


# ---------------------------------------------------------------------------
# download_transformed_data
# ---------------------------------------------------------------------------


class TestDownloadTransformedData:
    """Tests for download_transformed_data (path validation)."""

    async def test_empty_output_path_raises(self):
        """Empty output_path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="No output_path"):
            await download_transformed_data("")

    async def test_whitespace_output_path_raises(self):
        """Whitespace-only output_path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="No output_path"):
            await download_transformed_data("   ")

    async def test_downloads_from_s3(self):
        """Downloads transformed data from S3 to local path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch(
                    "application_sdk.common.incremental.state.state_writer.download_prefix",
                    new_callable=AsyncMock,
                ) as mock_store,
                patch(
                    "application_sdk.common.incremental.state.state_writer.get_object_store_prefix",
                    return_value="prefix/transformed",
                ),
            ):
                result = await download_transformed_data(temp_dir)

            assert result == Path(temp_dir) / "transformed"
            mock_store.assert_awaited_once()

    async def test_lands_files_where_transformed_readers_look(
        self, memory_store, tmp_path, monkeypatch
    ) -> None:
        """Downloaded bytes must land at ``<output_path>/transformed/<entity>/``.

        Run against a real store rather than a mocked download: the regression
        this guards (FND-340) was a *layout* bug, invisible to a mock asserting
        only that the download was awaited. The nested layout it produced —
        ``transformed/artifacts/.../transformed/table/`` — made
        ``get_current_table_scope`` return None and ``write_current_state``
        raise "No tables found in transformed output".
        """
        from application_sdk.execution._temporal import activity_utils

        staging = tmp_path / "staging"
        monkeypatch.setattr(activity_utils, "TEMPORARY_PATH", str(staging))
        output_path = staging / "artifacts/apps/oracle/workflows/wf-1/run-1"

        prefix = "artifacts/apps/oracle/workflows/wf-1/run-1/transformed"
        await _put(
            f"{prefix}/table/chunk-0.json", b'{"t": 1}', memory_store, normalize=False
        )
        await _put(
            f"{prefix}/column/chunk-0.json", b'{"c": 1}', memory_store, normalize=False
        )

        transformed_dir = await download_transformed_data(str(output_path))

        assert transformed_dir == output_path / "transformed"
        assert (transformed_dir / "table" / "chunk-0.json").read_bytes() == b'{"t": 1}'
        assert (transformed_dir / "column" / "chunk-0.json").read_bytes() == b'{"c": 1}'
        # The nested-layout signature must not reappear.
        assert not (transformed_dir / "artifacts").exists()


# ---------------------------------------------------------------------------
# _copy_columns_from_transformed
# ---------------------------------------------------------------------------


class TestCopyColumnsFromTransformed:
    """Tests for the lightweight column copy helper."""

    def test_returns_zero_when_column_dir_missing(self):
        """No column dir → returns 0 (no copy attempted)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"
            transformed.mkdir()
            count = _copy_columns_from_transformed(transformed, state)
            assert count == 0
            # No column dir created in destination
            assert not (state / "column").exists()

    def test_copies_columns(self):
        """Column files are copied via copy_directory_parallel."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"
            col = transformed / "column"
            col.mkdir(parents=True)
            (col / "chunk-0.json").write_text("{}")
            (col / "chunk-1.json").write_text("{}")

            count = _copy_columns_from_transformed(transformed, state)
            assert count == 2
            assert (state / "column" / "chunk-0.json").exists()
            assert (state / "column" / "chunk-1.json").exists()


# ---------------------------------------------------------------------------
# upload_current_state
# ---------------------------------------------------------------------------


class TestUploadCurrentState:
    """upload_current_state (deprecated) commits through CurrentStateStore."""

    async def test_the_upload_becomes_the_committed_snapshot(
        self, memory_store, tmp_path
    ):
        """Once a manifest exists, a plain upload would be invisible to every
        reader; the deprecated helper must commit instead."""
        store = CurrentStateStore.for_connection("default/oracle/123", "oracle")
        first = tmp_path / "first"
        (first / "table").mkdir(parents=True)
        (first / "table" / "chunk-0.json").write_text("old")
        await store.commit(first, "run-1")

        state_dir = tmp_path / "current-state"
        (state_dir / "table").mkdir(parents=True)
        (state_dir / "table" / "chunk-0.json").write_text("new")
        with pytest.warns(DeprecationWarning, match="upload_current_state"):
            result = await upload_current_state(
                state_dir,
                connection_qualified_name="default/oracle/123",
                application_name="oracle",
            )

        assert result == store.s3_prefix
        snapshot = await store.probe()
        assert snapshot.committed_run_id is not None
        assert snapshot.committed_run_id.startswith("legacy-upload-")
        dest = await store.materialize(snapshot, tmp_path / "prev")
        assert [p.read_text() for p in dest.rglob("*.json")] == ["new"]


# ---------------------------------------------------------------------------
# create_current_state_snapshot
# ---------------------------------------------------------------------------


def _make_scope_with_tables(qns: list[str]) -> TableScope:
    """Build a TableScope backed by a plain dict (no real RocksDB)."""
    scope = TableScope(table_states={})
    for qn in qns:
        add_table_to_scope(scope, qn, "CREATED")
    scope.state_counts = {"CREATED": len(qns)}
    return scope


class TestCreateCurrentStateSnapshot:
    """End-to-end orchestration tests for create_current_state_snapshot.

    All side effects (DuckDB, S3 upload, table-scope helpers, file copy,
    diff creation) are mocked so the test exercises only the orchestration
    logic and never touches a real connection or remote service.
    """

    async def _run(
        self,
        *,
        scope_qns: list[str] | None,
        previous_state_present: bool,
        get_backfill_tables_fn=None,
        stale_diff_dir_present: bool = False,
        upload_concurrency: int | None = None,
    ) -> CurrentStateResult | None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            current_state = Path(temp_dir) / "current-state"
            previous_state = Path(temp_dir) / "previous-state"
            diff_dir = Path(temp_dir) / "diff"

            if stale_diff_dir_present:
                # A prior run's incremental-diff, so the clear-and-recreate
                # branch (and its rmtree) actually executes.
                diff_dir.mkdir(parents=True)
                (diff_dir / "leftover.json").write_text("{}")

            # Build minimal entity dirs
            for entity in ("table", "column"):
                d = transformed / entity
                d.mkdir(parents=True)
                (d / "chunk-0.json").write_text("{}")

            if previous_state_present:
                previous_state.mkdir()

            scope = (
                _make_scope_with_tables(scope_qns) if scope_qns is not None else None
            )

            mock_diff = MagicMock(total_files=7)
            state_store = MagicMock(spec=CurrentStateStore)
            state_store.s3_prefix = "persistent/oracle/conn/123/current-state"
            state_store.commit = AsyncMock(return_value=MagicMock())

            with (
                patch(
                    "application_sdk.common.incremental.state.state_writer."
                    "DuckDBConnectionManager"
                ) as mock_dbm,
                patch(
                    "application_sdk.common.incremental.state.state_writer."
                    "get_current_table_scope",
                    return_value=scope,
                ),
                patch(
                    "application_sdk.common.incremental.state.state_writer."
                    "get_table_qns_from_columns",
                    return_value=set(scope_qns or []),
                ),
                patch(
                    "application_sdk.common.incremental.state.state_writer."
                    "close_scope"
                ) as mock_close_scope,
                patch(
                    "application_sdk.common.incremental.state.state_writer."
                    "create_incremental_diff",
                    return_value=mock_diff,
                ) as mock_create_diff,
                patch(
                    "application_sdk.common.incremental.state.state_writer."
                    "get_persistent_artifacts_path",
                    return_value=diff_dir,
                ),
                patch(
                    "application_sdk.common.incremental.state.state_writer."
                    "upload_prefix",
                    new_callable=AsyncMock,
                ) as mock_upload,
            ):
                # Simulate connection manager context-manager
                mock_dbm.return_value.__enter__.return_value.connection = MagicMock()

                snapshot_kwargs = (
                    {"upload_concurrency": upload_concurrency}
                    if upload_concurrency is not None
                    else {}
                )

                if scope is None:
                    with pytest.raises(FileNotFoundError, match="No tables found"):
                        await create_current_state_snapshot(
                            connection_qualified_name="default/oracle/123",
                            transformed_dir=transformed,
                            previous_state_dir=(
                                previous_state if previous_state_present else None
                            ),
                            current_state_dir=current_state,
                            s3_prefix="persistent/oracle/conn/123",
                            run_id="run-abc",
                            get_backfill_tables_fn=get_backfill_tables_fn,
                            state_store=state_store,
                            **snapshot_kwargs,
                        )
                    state_store.commit.assert_not_awaited()
                    return None

                result = await create_current_state_snapshot(
                    connection_qualified_name="default/oracle/123",
                    transformed_dir=transformed,
                    previous_state_dir=(
                        previous_state if previous_state_present else None
                    ),
                    current_state_dir=current_state,
                    s3_prefix="persistent/oracle/conn/123",
                    run_id="run-abc",
                    get_backfill_tables_fn=get_backfill_tables_fn,
                    state_store=state_store,
                    **snapshot_kwargs,
                )

            # close_scope must always be called when scope is created
            mock_close_scope.assert_called_once_with(scope)
            # The diff is a plain upload; current-state goes through one commit.
            expected_uploads = 1 if previous_state_present else 0
            assert mock_upload.await_count == expected_uploads
            state_store.commit.assert_awaited_once()
            assert state_store.commit.await_args.args == (current_state, "run-abc")
            self._last_upload_calls = list(mock_upload.call_args_list)
            self._last_commit = state_store.commit.await_args

            if previous_state_present:
                mock_create_diff.assert_called_once()
                assert (
                    mock_create_diff.call_args.kwargs["get_backfill_tables_fn"]
                    is get_backfill_tables_fn
                )
            else:
                mock_create_diff.assert_not_called()

            return result

    async def test_raises_when_no_tables_in_scope(self):
        """Empty transformed output → FileNotFoundError."""
        await self._run(scope_qns=None, previous_state_present=False)

    async def test_first_run_skips_diff_creation(self):
        """No previous state → no diff produced, only current-state uploaded."""
        result = await self._run(
            scope_qns=["db/s/t1", "db/s/t2"], previous_state_present=False
        )
        assert result is not None
        assert (
            result.current_state_s3_prefix == "persistent/oracle/conn/123/current-state"
        )
        assert result.incremental_diff_files == 0
        assert result.incremental_diff_dir is None

    async def test_incremental_run_creates_diff_and_passes_backfill_fn(self):
        """Previous state present → diff created and uploaded."""
        backfill_fn = MagicMock(return_value={"db/s/new"})
        result = await self._run(
            scope_qns=["db/s/t1"],
            previous_state_present=True,
            get_backfill_tables_fn=backfill_fn,
        )
        assert result is not None
        assert result.incremental_diff_s3_prefix == (
            "persistent/oracle/conn/123/runs/run-abc/incremental-diff"
        )
        assert result.incremental_diff_files == 7

    async def test_upload_concurrency_defaults_to_four(self):
        """No override → both uploads use upload_prefix's historical default."""
        result = await self._run(scope_qns=["db/s/t1"], previous_state_present=True)
        assert result is not None
        assert len(self._last_upload_calls) == 1
        assert self._last_upload_calls[0].kwargs["max_concurrency"] == 4
        assert self._last_commit.kwargs["max_concurrency"] == 4

    async def test_upload_concurrency_override_reaches_both_uploads(self):
        """Explicit upload_concurrency reaches both the diff and current-state
        upload_prefix calls -- the actual knob a large connection would raise
        to shrink wall-clock time on the upload step."""
        result = await self._run(
            scope_qns=["db/s/t1"],
            previous_state_present=True,
            upload_concurrency=16,
        )
        assert result is not None
        assert len(self._last_upload_calls) == 1
        assert self._last_upload_calls[0].kwargs["max_concurrency"] == 16
        assert self._last_commit.kwargs["max_concurrency"] == 16

    async def test_directory_prep_and_diff_clear_offloaded_to_thread(self):
        """Both tree removals inside the snapshot must be offloaded.

        The current-state reset and the incremental-diff clear are both
        offloaded at this call site. Either one running on the loop would stall the
        enclosing @task's auto-heartbeat for the tree's removal time.
        """
        offloaded: list[tuple[object, tuple]] = []

        async def _record(func, *args, **kwargs):
            offloaded.append((func, args))
            return func(*args, **kwargs)

        with patch(
            "application_sdk.common.incremental.state.state_writer.run_in_thread",
            side_effect=_record,
        ):
            result = await self._run(
                scope_qns=["db/s/t1"],
                previous_state_present=True,
                stale_diff_dir_present=True,
            )

        assert result is not None
        called = [func for func, _ in offloaded]
        assert (
            _reset_directory in called
        ), "current-state directory prep was not offloaded"
        diff_clears = [
            args
            for func, args in offloaded
            if func is shutil.rmtree and args and Path(args[0]).name == "diff"
        ]
        assert diff_clears, "stale incremental-diff clear was not offloaded"
