"""Unit tests for IncrementalSqlMetadataExtractor template."""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.app.task import is_task, task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef
from application_sdk.templates.contracts.incremental_sql import (
    ExecuteColumnBatchInput,
    ExecuteColumnBatchOutput,
    FetchColumnsIncrementalInput,
    FetchIncrementalMarkerInput,
    FetchIncrementalMarkerOutput,
    FetchTablesIncrementalInput,
    IncrementalExtractionInput,
    IncrementalExtractionOutput,
    IncrementalRunContext,
    IncrementalTaskInput,
    PrepareColumnQueriesInput,
    PrepareColumnQueriesOutput,
    ReadCurrentStateInput,
    ReadCurrentStateOutput,
    UpdateMarkerInput,
    UpdateMarkerOutput,
    WriteCurrentStateInput,
    WriteCurrentStateOutput,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionTaskInput,
    FetchColumnsOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesOutput,
)
from application_sdk.templates.incremental_sql_metadata_extractor import (
    IncrementalSqlMetadataExtractor,
)
from application_sdk.templates.sql_metadata_extractor import SqlMetadataExtractor

# ---------------------------------------------------------------------------
# Minimal concrete subclass for structural tests
# ---------------------------------------------------------------------------


class _MinimalIncremental(IncrementalSqlMetadataExtractor):
    @task(timeout_seconds=60)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        return FetchDatabasesOutput()

    @task(timeout_seconds=60)
    async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
        return FetchSchemasOutput()

    @task(timeout_seconds=60)
    async def fetch_tables(
        self, input: FetchTablesIncrementalInput
    ) -> FetchTablesOutput:
        return FetchTablesOutput()

    async def run(
        self, input: IncrementalExtractionInput
    ) -> IncrementalExtractionOutput:
        return IncrementalExtractionOutput()

    def build_incremental_column_sql(
        self, table_ids: list[str], ctx: IncrementalRunContext
    ) -> str:
        return "SELECT 1"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIncrementalRunContext:
    """Tests for IncrementalRunContext.is_incremental_ready()."""

    def test_false_when_incremental_extraction_disabled(self) -> None:
        ctx = IncrementalRunContext(
            incremental_extraction=False,
            marker_timestamp="2025-01-01T00:00:00Z",
            current_state_available=True,
        )
        assert ctx.is_incremental_ready() is False

    def test_false_when_no_marker(self) -> None:
        ctx = IncrementalRunContext(
            incremental_extraction=True,
            marker_timestamp=None,
            current_state_available=True,
        )
        assert ctx.is_incremental_ready() is False

    def test_false_when_current_state_unavailable(self) -> None:
        ctx = IncrementalRunContext(
            incremental_extraction=True,
            marker_timestamp="2025-01-01T00:00:00Z",
            current_state_available=False,
        )
        assert ctx.is_incremental_ready() is False

    def test_true_when_all_conditions_met(self) -> None:
        ctx = IncrementalRunContext(
            incremental_extraction=True,
            marker_timestamp="2025-01-01T00:00:00Z",
            current_state_available=True,
        )
        assert ctx.is_incremental_ready() is True

    def test_false_with_empty_string_marker(self) -> None:
        """Empty string marker should be treated as no marker."""
        ctx = IncrementalRunContext(
            incremental_extraction=True,
            marker_timestamp="",
            current_state_available=True,
        )
        assert ctx.is_incremental_ready() is False

    def test_is_plain_dataclass_not_input_output(self) -> None:
        assert dataclasses.is_dataclass(IncrementalRunContext)
        assert not issubclass(IncrementalRunContext, Input)
        assert not issubclass(IncrementalRunContext, Output)


class TestIncrementalSqlMetadataExtractorStructure:
    """Tests for IncrementalSqlMetadataExtractor class structure."""

    def test_is_subclass_of_sql_metadata_extractor(self) -> None:
        assert issubclass(IncrementalSqlMetadataExtractor, SqlMetadataExtractor)

    def test_has_fetch_incremental_marker_task(self) -> None:
        assert is_task(IncrementalSqlMetadataExtractor.fetch_incremental_marker)

    def test_has_read_current_state_task(self) -> None:
        assert is_task(IncrementalSqlMetadataExtractor.read_current_state)

    def test_has_prepare_column_extraction_queries_task(self) -> None:
        assert is_task(
            IncrementalSqlMetadataExtractor.prepare_column_extraction_queries
        )

    def test_has_execute_single_column_batch_task(self) -> None:
        assert is_task(IncrementalSqlMetadataExtractor.execute_single_column_batch)

    def test_has_write_current_state_task(self) -> None:
        assert is_task(IncrementalSqlMetadataExtractor.write_current_state)

    def test_has_update_incremental_marker_task(self) -> None:
        assert is_task(IncrementalSqlMetadataExtractor.update_incremental_marker)

    def test_has_fetch_columns_task(self) -> None:
        assert is_task(IncrementalSqlMetadataExtractor.fetch_columns)

    def test_has_fetch_tables_task(self) -> None:
        assert is_task(IncrementalSqlMetadataExtractor.fetch_tables)

    def test_run_accepts_incremental_extraction_input(self) -> None:
        from typing import get_type_hints

        # Use class's own __dict__ to avoid inheriting _original_run from SqlMetadataExtractor
        cls_dict = IncrementalSqlMetadataExtractor.__dict__
        run_fn = cls_dict.get("_original_run") or cls_dict.get("run")
        hints = get_type_hints(run_fn)
        assert hints.get("input") is IncrementalExtractionInput

    def test_run_returns_incremental_extraction_output(self) -> None:
        from typing import get_type_hints

        cls_dict = IncrementalSqlMetadataExtractor.__dict__
        run_fn = cls_dict.get("_original_run") or cls_dict.get("run")
        hints = get_type_hints(run_fn)
        assert hints.get("return") is IncrementalExtractionOutput


class TestFetchColumnsSkip:
    """Tests for the incremental skip logic in fetch_columns."""

    def _make_extractor(self) -> _MinimalIncremental:
        """Create a minimal concrete subclass instance for testing."""
        return _MinimalIncremental.__new__(_MinimalIncremental)

    def test_returns_zero_counts_when_incremental_ready(self) -> None:
        """fetch_columns must return FetchColumnsOutput() when incremental mode active."""
        extractor = self._make_extractor()
        inp = FetchColumnsIncrementalInput(
            marker_timestamp="2025-01-01T00:00:00Z",
            current_state_available=True,
        )
        result = asyncio.run(extractor.fetch_columns(inp))
        assert isinstance(result, FetchColumnsOutput)
        assert result.total_record_count == 0
        assert result.chunk_count == 0

    def test_raises_not_implemented_when_full_extraction(self) -> None:
        """fetch_columns must raise NotImplementedError on full extraction."""
        extractor = self._make_extractor()
        inp = FetchColumnsIncrementalInput(
            marker_timestamp="",
            current_state_available=False,
        )
        with pytest.raises(NotImplementedError):
            asyncio.run(extractor.fetch_columns(inp))

    def test_raises_not_implemented_when_marker_but_no_state(self) -> None:
        """fetch_columns must raise NotImplementedError if state not available."""
        extractor = self._make_extractor()
        inp = FetchColumnsIncrementalInput(
            marker_timestamp="2025-01-01T00:00:00Z",
            current_state_available=False,
        )
        with pytest.raises(NotImplementedError):
            asyncio.run(extractor.fetch_columns(inp))


class TestIncrementalExtractionInput:
    """Tests for IncrementalExtractionInput contract."""

    def test_default_incremental_extraction_false(self) -> None:
        inp = IncrementalExtractionInput()
        assert inp.incremental_extraction is False

    def test_default_column_batch_size(self) -> None:
        inp = IncrementalExtractionInput()
        assert inp.column_batch_size == 25000

    def test_inherits_workflow_id_from_extraction_input(self) -> None:
        inp = IncrementalExtractionInput(workflow_id="wf-123")
        assert inp.workflow_id == "wf-123"

    def test_no_workflow_args_field(self) -> None:
        assert "workflow_args" not in IncrementalExtractionInput.model_fields

    def test_is_subclass_of_extraction_input(self) -> None:
        assert issubclass(IncrementalExtractionInput, ExtractionInput)


class TestContractTypes:
    """Tests that all new contract types conform to the Input/Output hierarchy."""

    _input_types = [
        FetchIncrementalMarkerInput,
        ReadCurrentStateInput,
        PrepareColumnQueriesInput,
        ExecuteColumnBatchInput,
        WriteCurrentStateInput,
        UpdateMarkerInput,
        IncrementalExtractionInput,
        IncrementalTaskInput,
        FetchTablesIncrementalInput,
        FetchColumnsIncrementalInput,
    ]

    _output_types = [
        FetchIncrementalMarkerOutput,
        ReadCurrentStateOutput,
        PrepareColumnQueriesOutput,
        ExecuteColumnBatchOutput,
        WriteCurrentStateOutput,
        UpdateMarkerOutput,
        IncrementalExtractionOutput,
    ]

    def test_all_input_types_are_pydantic_models(self) -> None:
        from pydantic import BaseModel

        for cls in self._input_types:
            assert issubclass(cls, BaseModel), f"{cls.__name__} is not a BaseModel"

    def test_all_output_types_are_pydantic_models(self) -> None:
        from pydantic import BaseModel

        for cls in self._output_types:
            assert issubclass(cls, BaseModel), f"{cls.__name__} is not a BaseModel"

    def test_all_input_types_extend_input(self) -> None:
        for cls in self._input_types:
            assert issubclass(cls, Input), f"{cls.__name__} does not extend Input"

    def test_all_output_types_extend_output(self) -> None:
        for cls in self._output_types:
            assert issubclass(cls, Output), f"{cls.__name__} does not extend Output"

    def test_incremental_run_context_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(IncrementalRunContext)

    def test_incremental_run_context_not_input_or_output(self) -> None:
        assert not issubclass(IncrementalRunContext, Input)
        assert not issubclass(IncrementalRunContext, Output)

    def test_incremental_task_input_extends_extraction_task_input(self) -> None:
        assert issubclass(IncrementalTaskInput, ExtractionTaskInput)

    def test_fetch_tables_incremental_extends_incremental_task_input(self) -> None:
        assert issubclass(FetchTablesIncrementalInput, IncrementalTaskInput)

    def test_fetch_columns_incremental_extends_incremental_task_input(self) -> None:
        assert issubclass(FetchColumnsIncrementalInput, IncrementalTaskInput)

    def test_incremental_extraction_input_extends_extraction_input(self) -> None:
        assert issubclass(IncrementalExtractionInput, ExtractionInput)

    def test_incremental_extraction_output_extends_extraction_output(self) -> None:
        from application_sdk.templates.contracts.sql_metadata import ExtractionOutput

        assert issubclass(IncrementalExtractionOutput, ExtractionOutput)

    def test_no_workflow_args_in_task_inputs(self) -> None:
        for cls in self._input_types:
            assert (
                "workflow_args" not in cls.model_fields
            ), f"{cls.__name__} still has workflow_args field"


# ---------------------------------------------------------------------------
# BLDX-1129 anchor tests — exercise every inline import in the module.
# Each test below executes a code path that contains a function-local
# `import ...` statement; if the import is broken or renamed at runtime,
# the test fails. Patch at the *source* module so the inline import resolves
# the patched object.
# ---------------------------------------------------------------------------


def _make_extractor() -> _MinimalIncremental:
    """Build a _MinimalIncremental without invoking App registration."""
    return _MinimalIncremental.__new__(_MinimalIncremental)


class TestFetchIncrementalMarkerInlineImport:
    """Exercises the inline import in fetch_incremental_marker (line 358)."""

    async def test_calls_fetch_marker_from_storage_and_maps_output(self) -> None:
        extractor = _make_extractor()
        with patch(
            "application_sdk.common.incremental.marker.fetch_marker_from_storage",
            new=AsyncMock(
                return_value=("2025-01-01T00:00:00Z", "2025-02-01T00:00:00Z")
            ),
        ) as mock_fn:
            out = await extractor.fetch_incremental_marker(
                FetchIncrementalMarkerInput(
                    connection_qualified_name="default/test/c",
                    application_name="app",
                    existing_marker="",
                    prepone_enabled=True,
                    prepone_hours=3.0,
                )
            )
        assert isinstance(out, FetchIncrementalMarkerOutput)
        assert out.marker_timestamp == "2025-01-01T00:00:00Z"
        assert out.next_marker_timestamp == "2025-02-01T00:00:00Z"
        mock_fn.assert_awaited_once()

    async def test_first_run_returns_empty_marker_when_storage_returns_none(
        self,
    ) -> None:
        extractor = _make_extractor()
        with patch(
            "application_sdk.common.incremental.marker.fetch_marker_from_storage",
            new=AsyncMock(return_value=(None, "2025-02-01T00:00:00Z")),
        ):
            out = await extractor.fetch_incremental_marker(
                FetchIncrementalMarkerInput(
                    connection_qualified_name="c",
                    application_name="app",
                )
            )
        assert out.marker_timestamp == ""
        assert out.next_marker_timestamp == "2025-02-01T00:00:00Z"


class TestReadCurrentStateInlineImport:
    """Exercises the inline import in read_current_state (line 384)."""

    async def test_maps_download_result_to_output(self) -> None:
        extractor = _make_extractor()
        with patch(
            "application_sdk.common.incremental.state.state_reader.download_current_state",
            new=AsyncMock(return_value=("/tmp/state", "s3://bucket/state", True, 7)),
        ) as mock_fn:
            out = await extractor.read_current_state(
                ReadCurrentStateInput(
                    connection_qualified_name="c", application_name="app"
                )
            )
        assert isinstance(out, ReadCurrentStateOutput)
        assert out.current_state_path == "/tmp/state"
        assert out.current_state_s3_prefix == "s3://bucket/state"
        assert out.current_state_available is True
        assert out.current_state_json_count == 7
        mock_fn.assert_awaited_once()

    async def test_first_run_state_unavailable(self) -> None:
        extractor = _make_extractor()
        with patch(
            "application_sdk.common.incremental.state.state_reader.download_current_state",
            new=AsyncMock(return_value=("/tmp/state", "", False, 0)),
        ):
            out = await extractor.read_current_state(
                ReadCurrentStateInput(
                    connection_qualified_name="c", application_name="app"
                )
            )
        assert out.current_state_available is False
        assert out.current_state_json_count == 0


class TestUpdateIncrementalMarkerInlineImport:
    """Exercises the inline import in update_incremental_marker (line 775)."""

    async def test_empty_next_marker_returns_early_no_import(self) -> None:
        """When next_marker is empty, no inline import should be reached."""
        extractor = _make_extractor()
        out = await extractor.update_incremental_marker(
            UpdateMarkerInput(
                connection_qualified_name="c",
                next_marker_timestamp="",
                application_name="app",
            )
        )
        assert out.marker_written is False

    async def test_persists_marker_via_inline_import(self) -> None:
        extractor = _make_extractor()
        with patch(
            "application_sdk.common.incremental.marker.persist_marker_to_storage",
            new=AsyncMock(
                return_value={
                    "marker_written": True,
                    "marker_timestamp": "2025-03-01T00:00:00Z",
                    "s3_key": "s3://bucket/markers/m.json",
                }
            ),
        ) as mock_fn:
            out = await extractor.update_incremental_marker(
                UpdateMarkerInput(
                    connection_qualified_name="c",
                    next_marker_timestamp="2025-03-01T00:00:00Z",
                    application_name="app",
                )
            )
        assert out.marker_written is True
        assert out.marker_timestamp == "2025-03-01T00:00:00Z"
        assert out.s3_key == "s3://bucket/markers/m.json"
        mock_fn.assert_awaited_once()

    async def test_persist_returns_minimal_dict_uses_defaults(self) -> None:
        """If helper returns dict missing keys, .get() defaults apply."""
        extractor = _make_extractor()
        with patch(
            "application_sdk.common.incremental.marker.persist_marker_to_storage",
            new=AsyncMock(return_value={}),
        ):
            out = await extractor.update_incremental_marker(
                UpdateMarkerInput(
                    connection_qualified_name="c",
                    next_marker_timestamp="2025-03-01T00:00:00Z",
                    application_name="app",
                )
            )
        assert out.marker_written is False
        assert out.marker_timestamp == ""
        assert out.s3_key == ""


class TestExecuteSingleColumnBatchInlineImports:
    """Exercises inline imports in execute_single_column_batch (lines 578-584)."""

    async def test_raises_when_output_path_missing(self) -> None:
        extractor = _make_extractor()
        with pytest.raises(ValueError, match="output_path"):
            await extractor.execute_single_column_batch(
                ExecuteColumnBatchInput(output_path="", batch_index=0, total_batches=1)
            )

    async def test_executes_batch_happy_path(self, tmp_path) -> None:
        extractor = _make_extractor()
        # Pre-write a batch file the helper would download
        batches_dir = tmp_path / "batches" / "column-table-ids"
        batches_dir.mkdir(parents=True)
        batch_file = batches_dir / "batch-0.json"
        batch_file.write_text('["t1", "t2", "t3"]', encoding="utf-8")

        async def fake_execute_column_sql(self, sql, inp, ctx):  # noqa: ARG001
            return 42

        # bind execute_column_sql at instance level
        extractor.execute_column_sql = AsyncMock(return_value=42)

        with (
            patch(
                "application_sdk.execution.get_object_store_prefix",
                return_value="s3://prefix/batches",
            ),
            patch(
                "application_sdk.storage.ops.download_file",
                new=AsyncMock(return_value=None),
            ) as mock_download,
        ):
            out = await extractor.execute_single_column_batch(
                ExecuteColumnBatchInput(
                    output_path=str(tmp_path),
                    batch_index=0,
                    total_batches=1,
                    batches_s3_prefix="s3://explicit/batches",
                    application_name="app",
                )
            )
        assert isinstance(out, ExecuteColumnBatchOutput)
        assert out.batch_index == 0
        assert out.records == 42
        assert out.status == "success"
        # Build sql received the table_ids list
        extractor.execute_column_sql.assert_awaited_once()
        mock_download.assert_awaited_once()

    async def test_batch_file_missing_returns_not_found(self, tmp_path) -> None:
        """If download succeeds but file doesn't exist on disk, status==not_found."""
        extractor = _make_extractor()
        # download_file returns but does NOT create the file
        with (
            patch(
                "application_sdk.execution.get_object_store_prefix",
                return_value="s3://prefix/batches",
            ),
            patch(
                "application_sdk.storage.ops.download_file",
                new=AsyncMock(return_value=None),
            ),
        ):
            out = await extractor.execute_single_column_batch(
                ExecuteColumnBatchInput(
                    output_path=str(tmp_path),
                    batch_index=5,
                    total_batches=10,
                    application_name="app",
                )
            )
        assert out.status == "not_found"
        assert out.batch_index == 5
        assert out.records == 0

    async def test_default_execute_column_sql_raises_not_implemented(
        self, tmp_path
    ) -> None:
        """Default execute_column_sql raises NotImplementedError; batch propagates."""
        extractor = _make_extractor()
        # Don't override execute_column_sql — use default
        batches_dir = tmp_path / "batches" / "column-table-ids"
        batches_dir.mkdir(parents=True)
        (batches_dir / "batch-0.json").write_text('["t1"]', encoding="utf-8")

        with (
            patch(
                "application_sdk.execution.get_object_store_prefix",
                return_value="s3://prefix/batches",
            ),
            patch(
                "application_sdk.storage.ops.download_file",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(NotImplementedError, match="execute_column_sql"),
        ):
            await extractor.execute_single_column_batch(
                ExecuteColumnBatchInput(
                    output_path=str(tmp_path),
                    batch_index=0,
                    total_batches=1,
                )
            )


class TestPrepareColumnExtractionQueriesInlineImports:
    """Exercises inline imports in prepare_column_extraction_queries
    (lines 413-429)."""

    async def test_raises_when_output_path_missing(self) -> None:
        extractor = _make_extractor()
        with pytest.raises(FileNotFoundError, match="output_path"):
            await extractor.prepare_column_extraction_queries(
                PrepareColumnQueriesInput(
                    output_path="",
                    connection_qualified_name="c",
                    application_name="app",
                )
            )

    async def test_zero_tables_returns_empty_output(self, tmp_path) -> None:
        """When no tables need extraction, returns total_batches=0 and skips upload."""
        extractor = _make_extractor()
        with (
            patch(
                "application_sdk.execution.get_object_store_prefix",
                return_value="s3://prefix/transformed",
            ),
            patch(
                "application_sdk.storage.batch.download_prefix",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "application_sdk.storage.batch.upload_prefix",
                new=AsyncMock(return_value=None),
            ) as mock_upload,
            patch(
                "application_sdk.common.incremental.column_extraction.get_backfill_tables",
                return_value=set(),
            ),
            patch(
                "application_sdk.common.incremental.column_extraction.get_tables_needing_column_extraction",
                return_value=(MagicMock(), 0, 0, 0),
            ),
        ):
            out = await extractor.prepare_column_extraction_queries(
                PrepareColumnQueriesInput(
                    output_path=str(tmp_path),
                    column_batch_size=10,
                    connection_qualified_name="c",
                    application_name="app",
                    current_state_available=False,
                )
            )
        assert out.total_batches == 0
        assert out.changed_tables == 0
        assert out.backfill_tables == 0
        assert out.total_tables == 0
        # No upload happened because there were no batches
        mock_upload.assert_not_awaited()

    async def test_batches_tables_and_uploads(self, tmp_path) -> None:
        """Happy path: tables exist → batched → JSON files written → uploaded."""
        extractor = _make_extractor()

        # Fake daft DataFrame: filtered_df.select("table_id").iter_rows()
        fake_df = MagicMock()
        select_result = MagicMock()
        select_result.iter_rows.return_value = iter(
            [{"table_id": f"t{i}"} for i in range(5)]
        )
        fake_df.select.return_value = select_result

        with (
            patch(
                "application_sdk.execution.get_object_store_prefix",
                side_effect=["s3://prefix/transformed", "s3://prefix/batches"],
            ),
            patch(
                "application_sdk.storage.batch.download_prefix",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "application_sdk.storage.batch.upload_prefix",
                new=AsyncMock(return_value=None),
            ) as mock_upload,
            patch(
                "application_sdk.common.incremental.column_extraction.get_backfill_tables",
                return_value={"t1"},
            ),
            patch(
                "application_sdk.common.incremental.column_extraction.get_tables_needing_column_extraction",
                return_value=(fake_df, 4, 1, 0),
            ),
        ):
            out = await extractor.prepare_column_extraction_queries(
                PrepareColumnQueriesInput(
                    output_path=str(tmp_path),
                    column_batch_size=2,  # 5 rows / 2 per batch -> 3 batches
                    connection_qualified_name="c",
                    application_name="app",
                    current_state_available=False,
                )
            )
        assert out.total_batches == 3
        assert out.changed_tables == 4
        assert out.backfill_tables == 1
        assert out.total_tables == 5
        mock_upload.assert_awaited_once()

        # Verify batch files were written
        batches_dir = tmp_path / "batches" / "column-table-ids"
        assert batches_dir.exists()
        files = sorted(batches_dir.glob("batch-*.json"))
        assert len(files) == 3

    async def test_download_state_failure_swallowed_logs_warning(
        self, tmp_path
    ) -> None:
        """If state download for backfill comparison fails, code logs and continues
        with previous_current_state_dir = None (lines 479-484)."""
        extractor = _make_extractor()

        fake_df = MagicMock()
        select_result = MagicMock()
        select_result.iter_rows.return_value = iter([])
        fake_df.select.return_value = select_result

        # Make get_persistent_artifacts_path return a path that exists but no
        # table dir / no json files, so the download branch is taken
        artifacts_dir = tmp_path / "persistent"
        artifacts_dir.mkdir()

        with (
            patch(
                "application_sdk.execution.get_object_store_prefix",
                return_value="s3://prefix",
            ),
            patch(
                "application_sdk.storage.batch.download_prefix",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "application_sdk.common.incremental.helpers.get_persistent_artifacts_path",
                return_value=artifacts_dir,
            ),
            patch(
                "application_sdk.common.incremental.helpers.download_s3_prefix_with_structure",
                new=AsyncMock(side_effect=RuntimeError("S3 failure")),
            ) as mock_dl_state,
            patch(
                "application_sdk.common.incremental.column_extraction.get_backfill_tables",
                return_value=set(),
            ),
            patch(
                "application_sdk.common.incremental.column_extraction.get_tables_needing_column_extraction",
                return_value=(fake_df, 0, 0, 0),
            ),
        ):
            out = await extractor.prepare_column_extraction_queries(
                PrepareColumnQueriesInput(
                    output_path=str(tmp_path),
                    column_batch_size=10,
                    connection_qualified_name="c",
                    application_name="app",
                    current_state_available=True,
                    current_state_s3_prefix="s3://state",
                )
            )
        # Failure was swallowed; flow continued with no batches
        assert out.total_batches == 0
        mock_dl_state.assert_awaited_once()


class TestWriteCurrentStateInlineImports:
    """Exercises inline imports in write_current_state (lines 694-703)."""

    def _make_input(self, **overrides) -> WriteCurrentStateInput:
        defaults = dict(
            workflow_id="wf",
            workflow_run_id="run",
            connection=ConnectionRef(
                attributes=ConnectionAttributes(
                    qualified_name="default/test/c", name="c"
                )
            ),
            output_path="/tmp/out",
            current_state_available=True,
            current_state_s3_prefix="s3://state",
            copy_workers=2,
            application_name="app",
        )
        defaults.update(overrides)
        return WriteCurrentStateInput(**defaults)

    async def test_happy_path_calls_helpers_and_returns_output(self, tmp_path) -> None:
        extractor = _make_extractor()

        # Build the expected snapshot result
        snap_result = MagicMock()
        snap_result.current_state_dir = tmp_path / "current"
        snap_result.current_state_s3_prefix = "s3://current"
        snap_result.total_files = 5
        snap_result.incremental_diff_dir = tmp_path / "diff"
        snap_result.incremental_diff_s3_prefix = "s3://diff"
        snap_result.incremental_diff_files = 2

        with (
            patch(
                "application_sdk.common.incremental.helpers.get_persistent_artifacts_path",
                return_value=tmp_path / "current",
            ),
            patch(
                "application_sdk.common.incremental.helpers.get_persistent_s3_prefix",
                return_value="s3://persist",
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.cleanup_previous_state",
            ) as mock_cleanup,
            patch(
                "application_sdk.common.incremental.state.state_writer.create_current_state_snapshot",
                new=AsyncMock(return_value=snap_result),
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.download_transformed_data",
                new=AsyncMock(return_value=tmp_path / "transformed"),
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.prepare_previous_state",
                new=AsyncMock(return_value=tmp_path / "prev"),
            ),
        ):
            out = await extractor.write_current_state(self._make_input())
        assert isinstance(out, WriteCurrentStateOutput)
        assert out.current_state_files == 5
        assert out.incremental_diff_files == 2
        assert "current" in out.current_state_path
        mock_cleanup.assert_called_once()

    async def test_empty_diff_dir_yields_empty_string(self, tmp_path) -> None:
        extractor = _make_extractor()
        snap_result = MagicMock()
        snap_result.current_state_dir = tmp_path / "current"
        snap_result.current_state_s3_prefix = "s3://current"
        snap_result.total_files = 0
        snap_result.incremental_diff_dir = None
        snap_result.incremental_diff_s3_prefix = None
        snap_result.incremental_diff_files = 0

        with (
            patch(
                "application_sdk.common.incremental.helpers.get_persistent_artifacts_path",
                return_value=tmp_path / "current",
            ),
            patch(
                "application_sdk.common.incremental.helpers.get_persistent_s3_prefix",
                return_value="s3://persist",
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.cleanup_previous_state",
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.create_current_state_snapshot",
                new=AsyncMock(return_value=snap_result),
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.download_transformed_data",
                new=AsyncMock(return_value=tmp_path / "transformed"),
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.prepare_previous_state",
                new=AsyncMock(return_value=None),
            ),
        ):
            out = await extractor.write_current_state(self._make_input())
        assert out.incremental_diff_path == ""
        assert out.incremental_diff_s3_prefix == ""

    async def test_exception_rewraps_and_cleans_up(self, tmp_path) -> None:
        """Exception inside try-block is rewrapped; finally still cleans up."""
        extractor = _make_extractor()

        with (
            patch(
                "application_sdk.common.incremental.helpers.get_persistent_artifacts_path",
                return_value=tmp_path / "current",
            ),
            patch(
                "application_sdk.common.incremental.helpers.get_persistent_s3_prefix",
                return_value="s3://persist",
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.cleanup_previous_state",
            ) as mock_cleanup,
            patch(
                "application_sdk.common.incremental.state.state_writer.create_current_state_snapshot",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.download_transformed_data",
                new=AsyncMock(return_value=tmp_path / "transformed"),
            ),
            patch(
                "application_sdk.common.incremental.state.state_writer.prepare_previous_state",
                new=AsyncMock(return_value=tmp_path / "prev"),
            ),
        ):
            with pytest.raises(Exception):
                await extractor.write_current_state(self._make_input())
        # cleanup_previous_state runs in finally
        mock_cleanup.assert_called_once()


class TestResolveDatabasePlaceholdersDefault:
    """resolve_database_placeholders is a no-op by default."""

    def test_returns_sql_unchanged(self) -> None:
        extractor = _make_extractor()
        sql = "SELECT * FROM {system_schema}.tables"
        out = extractor.resolve_database_placeholders(
            sql,
            FetchTablesIncrementalInput(),
        )
        assert out == sql


class TestRunOrchestration:
    """End-to-end run() orchestration with all tasks stubbed.

    Patches temporalio.workflow.info() (defensive inline import on line 803)
    and stubs every @task method on the instance.
    """

    def _build_extractor(self):
        """Construct a _MinimalIncremental, stubbing every task with AsyncMock."""
        ext = _make_extractor()
        ext.fetch_incremental_marker = AsyncMock(
            return_value=FetchIncrementalMarkerOutput(
                marker_timestamp="", next_marker_timestamp="2025-04-01T00:00:00Z"
            )
        )
        ext.read_current_state = AsyncMock(
            return_value=ReadCurrentStateOutput(
                current_state_path="/tmp/state",
                current_state_s3_prefix="s3://state",
                current_state_available=False,
                current_state_json_count=0,
            )
        )
        ext.fetch_databases = AsyncMock(
            return_value=FetchDatabasesOutput(total_record_count=2)
        )
        ext.fetch_schemas = AsyncMock(
            return_value=FetchSchemasOutput(total_record_count=3)
        )
        ext.fetch_tables = AsyncMock(
            return_value=FetchTablesOutput(total_record_count=10)
        )
        ext.fetch_columns = AsyncMock(
            return_value=FetchColumnsOutput(total_record_count=100)
        )
        ext.prepare_column_extraction_queries = AsyncMock(
            return_value=PrepareColumnQueriesOutput(
                total_batches=0, changed_tables=0, backfill_tables=0, total_tables=0
            )
        )
        ext.execute_single_column_batch = AsyncMock(
            return_value=ExecuteColumnBatchOutput(
                batch_index=0, records=0, status="success"
            )
        )
        ext.write_current_state = AsyncMock(
            return_value=WriteCurrentStateOutput(
                current_state_path="/tmp/cs",
                current_state_s3_prefix="s3://cs",
                current_state_files=5,
                incremental_diff_path="",
                incremental_diff_s3_prefix="",
                incremental_diff_files=0,
            )
        )
        ext.update_incremental_marker = AsyncMock(
            return_value=UpdateMarkerOutput(marker_written=True)
        )
        return ext

    def _input(self, **overrides) -> IncrementalExtractionInput:
        defaults = dict(
            workflow_id="wf-1",
            connection=ConnectionRef(
                attributes=ConnectionAttributes(
                    qualified_name="default/test/c", name="c"
                )
            ),
            output_path="/tmp/out",
            output_prefix="/tmp",
            incremental_extraction=False,
        )
        defaults.update(overrides)
        return IncrementalExtractionInput(**defaults)

    @staticmethod
    def _patch_workflow_info():
        info = MagicMock()
        info.run_id = "run-xyz"
        return patch("temporalio.workflow.info", return_value=info)

    async def test_full_run_first_run_no_incremental(self) -> None:
        """First run: no incremental-ready, no batches, no marker update."""
        # Use base class so we exercise the real run() (not _MinimalIncremental.run override)
        # We re-implement by calling real run() via super-bypass
        ext = self._build_extractor()
        with self._patch_workflow_info():
            out = await IncrementalSqlMetadataExtractor.run(ext, self._input())
        assert isinstance(out, IncrementalExtractionOutput)
        assert out.success is True
        assert out.databases_extracted == 2
        assert out.schemas_extracted == 3
        assert out.tables_extracted == 10
        # incremental_extraction=False so update_marker not called
        assert out.marker_updated is False
        ext.update_incremental_marker.assert_not_awaited()
        # Not incremental_ready so no batch prep
        ext.prepare_column_extraction_queries.assert_not_awaited()

    async def test_run_with_incremental_ready_executes_batches(self) -> None:
        ext = self._build_extractor()
        # Marker exists + state available -> incremental_ready
        ext.fetch_incremental_marker = AsyncMock(
            return_value=FetchIncrementalMarkerOutput(
                marker_timestamp="2025-01-01T00:00:00Z",
                next_marker_timestamp="2025-02-01T00:00:00Z",
            )
        )
        ext.read_current_state = AsyncMock(
            return_value=ReadCurrentStateOutput(
                current_state_path="/tmp/state",
                current_state_s3_prefix="s3://state",
                current_state_available=True,
                current_state_json_count=10,
            )
        )
        ext.prepare_column_extraction_queries = AsyncMock(
            return_value=PrepareColumnQueriesOutput(
                total_batches=3,
                changed_tables=15,
                backfill_tables=2,
                total_tables=17,
                batches_s3_prefix="s3://batches",
            )
        )
        ext.execute_single_column_batch = AsyncMock(
            side_effect=[
                ExecuteColumnBatchOutput(batch_index=i, records=10, status="success")
                for i in range(3)
            ]
        )

        with self._patch_workflow_info():
            out = await IncrementalSqlMetadataExtractor.run(
                ext, self._input(incremental_extraction=True)
            )
        assert out.column_batches_executed == 3
        assert out.columns_extracted == 30
        assert out.changed_tables == 15
        assert out.backfill_tables == 2
        assert out.marker_updated is True
        ext.update_incremental_marker.assert_awaited_once()

    async def test_run_chunks_batches_above_max_concurrent(self) -> None:
        """When total_batches > MAX_CONCURRENT_COLUMN_BATCHES (10), run in chunks."""
        from application_sdk.templates.incremental_sql_metadata_extractor import (
            MAX_CONCURRENT_COLUMN_BATCHES,
        )

        assert MAX_CONCURRENT_COLUMN_BATCHES == 10
        ext = self._build_extractor()
        ext.fetch_incremental_marker = AsyncMock(
            return_value=FetchIncrementalMarkerOutput(
                marker_timestamp="2025-01-01T00:00:00Z",
                next_marker_timestamp="2025-02-01T00:00:00Z",
            )
        )
        ext.read_current_state = AsyncMock(
            return_value=ReadCurrentStateOutput(
                current_state_path="/tmp/state",
                current_state_s3_prefix="s3://state",
                current_state_available=True,
                current_state_json_count=10,
            )
        )
        ext.prepare_column_extraction_queries = AsyncMock(
            return_value=PrepareColumnQueriesOutput(
                total_batches=12, changed_tables=12, backfill_tables=0, total_tables=12
            )
        )
        ext.execute_single_column_batch = AsyncMock(
            side_effect=[
                ExecuteColumnBatchOutput(batch_index=i, records=1, status="success")
                for i in range(12)
            ]
        )

        with self._patch_workflow_info():
            out = await IncrementalSqlMetadataExtractor.run(
                ext, self._input(incremental_extraction=True)
            )
        # 12 batches in chunks of 10 -> 2 chunks, 12 total awaits
        assert ext.execute_single_column_batch.await_count == 12
        assert out.column_batches_executed == 12
        assert out.columns_extracted == 12

    async def test_run_uses_legacy_credential_guid_inline_import(self) -> None:
        """When credential_ref is None but credential_guid is set, run() inline-imports
        legacy_credential_ref (line 813)."""
        from application_sdk.credentials.ref import CredentialRef

        ext = self._build_extractor()
        fake_ref = CredentialRef(credential_guid="some-guid")
        with (
            patch(
                "application_sdk.credentials.legacy_credential_ref",
                return_value=fake_ref,
            ) as mock_legacy,
            self._patch_workflow_info(),
        ):
            await IncrementalSqlMetadataExtractor.run(
                ext,
                self._input(credential_guid="some-guid"),
            )
        mock_legacy.assert_called_once_with("some-guid")

    async def test_run_does_not_update_marker_when_no_next_marker(self) -> None:
        ext = self._build_extractor()
        ext.fetch_incremental_marker = AsyncMock(
            return_value=FetchIncrementalMarkerOutput(
                marker_timestamp="", next_marker_timestamp=""
            )
        )
        with self._patch_workflow_info():
            out = await IncrementalSqlMetadataExtractor.run(
                ext, self._input(incremental_extraction=True)
            )
        ext.update_incremental_marker.assert_not_awaited()
        assert out.marker_updated is False

    async def test_run_skips_batches_when_total_batches_zero(self) -> None:
        """is_incremental_ready True but prepare returns 0 batches → no execute calls."""
        ext = self._build_extractor()
        ext.fetch_incremental_marker = AsyncMock(
            return_value=FetchIncrementalMarkerOutput(
                marker_timestamp="2025-01-01T00:00:00Z",
                next_marker_timestamp="2025-02-01T00:00:00Z",
            )
        )
        ext.read_current_state = AsyncMock(
            return_value=ReadCurrentStateOutput(
                current_state_path="/tmp/state",
                current_state_s3_prefix="s3://state",
                current_state_available=True,
                current_state_json_count=10,
            )
        )
        # prep returns 0 batches
        ext.prepare_column_extraction_queries = AsyncMock(
            return_value=PrepareColumnQueriesOutput(
                total_batches=0, changed_tables=0, backfill_tables=0, total_tables=0
            )
        )
        ext.fetch_columns = AsyncMock(
            return_value=FetchColumnsOutput(total_record_count=42)
        )
        with self._patch_workflow_info():
            out = await IncrementalSqlMetadataExtractor.run(
                ext, self._input(incremental_extraction=True)
            )
        ext.execute_single_column_batch.assert_not_awaited()
        assert out.column_batches_executed == 0
        # Falls back to fetch_columns total
        assert out.columns_extracted == 42
