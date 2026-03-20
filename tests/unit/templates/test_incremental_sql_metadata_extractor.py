"""Unit tests for IncrementalSqlMetadataExtractor template."""

from __future__ import annotations

import asyncio
import dataclasses
import warnings

import pytest

from application_sdk.app.task import is_task, task
from application_sdk.contracts.base import Input, Output
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
        field_names = {f.name for f in dataclasses.fields(IncrementalExtractionInput)}
        assert "workflow_args" not in field_names

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

    def test_all_input_types_are_dataclasses(self) -> None:
        for cls in self._input_types:
            assert dataclasses.is_dataclass(cls), f"{cls.__name__} is not a dataclass"

    def test_all_output_types_are_dataclasses(self) -> None:
        for cls in self._output_types:
            assert dataclasses.is_dataclass(cls), f"{cls.__name__} is not a dataclass"

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
            field_names = {f.name for f in dataclasses.fields(cls)}
            assert (
                "workflow_args" not in field_names
            ), f"{cls.__name__} still has workflow_args field"


class TestV2DeprecationWarning:
    """Tests that importing the v2 incremental module emits a DeprecationWarning."""

    def test_importing_incremental_activities_emits_deprecation(self) -> None:
        import importlib
        import sys

        # Remove the module from cache so the warning fires again
        mod_name = "application_sdk.activities.metadata_extraction.incremental"
        sys.modules.pop(mod_name, None)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            importlib.import_module(mod_name)

        deprecation_warnings = [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]
        assert len(deprecation_warnings) >= 1, (
            "Expected at least one DeprecationWarning when importing "
            "application_sdk.activities.metadata_extraction.incremental"
        )
        assert (
            "IncrementalSqlMetadataExtractor" in str(deprecation_warnings[0].message)
            or "deprecated" in str(deprecation_warnings[0].message).lower()
        )
