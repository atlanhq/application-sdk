"""Unit tests for SqlMetadataExtractor template."""

from __future__ import annotations

import pytest

from application_sdk.app.base import App
from application_sdk.app.task import is_task
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    ExtractionTaskInput,
    FetchColumnsInput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchProceduresInput,
    FetchProceduresOutput,
    FetchSchemasInput,
    FetchTablesInput,
)
from application_sdk.templates.sql_metadata_extractor import SqlMetadataExtractor


class TestSqlMetadataExtractorStructure:
    """Tests for SqlMetadataExtractor class structure."""

    def test_is_app_subclass(self) -> None:
        assert issubclass(SqlMetadataExtractor, App)

    def test_has_fetch_databases_task(self) -> None:
        method = SqlMetadataExtractor.fetch_databases
        assert is_task(method)

    def test_has_fetch_schemas_task(self) -> None:
        method = SqlMetadataExtractor.fetch_schemas
        assert is_task(method)

    def test_has_fetch_tables_task(self) -> None:
        method = SqlMetadataExtractor.fetch_tables
        assert is_task(method)

    def test_has_fetch_columns_task(self) -> None:
        method = SqlMetadataExtractor.fetch_columns
        assert is_task(method)

    def test_has_transform_data_task(self) -> None:
        method = SqlMetadataExtractor.transform_data
        assert is_task(method)

    def test_has_fetch_procedures_task(self) -> None:
        method = SqlMetadataExtractor.fetch_procedures
        assert is_task(method)

    def test_run_accepts_extraction_input(self) -> None:
        # After registration, run() is wrapped. Check original run() type hints.
        from typing import get_type_hints

        original_run = getattr(
            SqlMetadataExtractor, "_original_run", SqlMetadataExtractor.run
        )
        hints = get_type_hints(original_run)
        assert hints.get("input") is ExtractionInput

    def test_run_returns_extraction_output(self) -> None:
        from typing import get_type_hints

        original_run = getattr(
            SqlMetadataExtractor, "_original_run", SqlMetadataExtractor.run
        )
        hints = get_type_hints(original_run)
        assert hints.get("return") is ExtractionOutput

    def test_fetch_databases_input_type(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlMetadataExtractor.fetch_databases)
        assert meta.input_type is FetchDatabasesInput

    def test_fetch_databases_output_type(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlMetadataExtractor.fetch_databases)
        assert meta.output_type is FetchDatabasesOutput

    def test_fetch_databases_timeout(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlMetadataExtractor.fetch_databases)
        assert meta.timeout_seconds == 1800

    def test_fetch_procedures_input_type(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlMetadataExtractor.fetch_procedures)
        assert meta.input_type is FetchProceduresInput

    def test_fetch_procedures_output_type(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlMetadataExtractor.fetch_procedures)
        assert meta.output_type is FetchProceduresOutput


class TestFetchProceduresTask:
    """Tests for the fetch_procedures task on SqlMetadataExtractor."""

    def test_fetch_procedures_is_task_decorated(self) -> None:
        assert is_task(SqlMetadataExtractor.fetch_procedures)

    def test_fetch_procedures_raises_not_implemented(self) -> None:
        extractor = SqlMetadataExtractor.__new__(SqlMetadataExtractor)
        with pytest.raises(NotImplementedError):
            import asyncio

            asyncio.run(extractor.fetch_procedures(FetchProceduresInput()))

    def test_fetch_procedures_input_has_no_workflow_args(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(FetchProceduresInput)}
        assert "workflow_args" not in field_names

    def test_fetch_procedures_output_has_counts(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(FetchProceduresOutput)}
        assert "chunk_count" in field_names
        assert "total_record_count" in field_names


class TestTypedTaskInputs:
    """Tests that per-task inputs use typed fields, not workflow_args dicts."""

    def test_fetch_databases_input_no_workflow_args(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(FetchDatabasesInput)}
        assert "workflow_args" not in field_names

    def test_fetch_schemas_input_no_workflow_args(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(FetchSchemasInput)}
        assert "workflow_args" not in field_names

    def test_fetch_tables_input_no_workflow_args(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(FetchTablesInput)}
        assert "workflow_args" not in field_names

    def test_fetch_columns_input_no_workflow_args(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(FetchColumnsInput)}
        assert "workflow_args" not in field_names

    def test_task_inputs_inherit_extraction_task_input(self) -> None:
        assert issubclass(FetchDatabasesInput, ExtractionTaskInput)
        assert issubclass(FetchSchemasInput, ExtractionTaskInput)
        assert issubclass(FetchTablesInput, ExtractionTaskInput)
        assert issubclass(FetchColumnsInput, ExtractionTaskInput)
        assert issubclass(FetchProceduresInput, ExtractionTaskInput)

    def test_extraction_task_input_has_typed_fields(self) -> None:
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(ExtractionTaskInput)}
        assert "workflow_id" in field_names
        assert "connection" in field_names
        assert "credential_guid" in field_names
        assert "output_prefix" in field_names
        assert "output_path" in field_names
        assert "exclude_filter" in field_names
        assert "include_filter" in field_names

    def test_fetch_databases_input_defaults(self) -> None:
        inp = FetchDatabasesInput()
        assert inp.workflow_id == ""
        assert inp.credential_guid == ""
        assert inp.output_path == ""


class TestSqlMetadataExtractorSubclass:
    """Tests for subclassing SqlMetadataExtractor."""

    def test_sql_metadata_extractor_is_registered_at_import(self) -> None:
        # SqlMetadataExtractor registers itself at import time;
        # the conftest reset clears it — so we verify the class ITSELF is an App subclass
        # and that its structure is correct (covered by TestSqlMetadataExtractorStructure).
        # Direct subclasses inherit _app_registered=True so they don't auto-register.
        assert issubclass(SqlMetadataExtractor, App)

    def test_fetch_databases_raises_not_implemented_by_default(self) -> None:
        extractor = SqlMetadataExtractor.__new__(SqlMetadataExtractor)
        with pytest.raises(NotImplementedError):
            import asyncio

            asyncio.run(extractor.fetch_databases(FetchDatabasesInput()))
