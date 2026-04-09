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
from application_sdk.templates.sql_metadata_extractor import (
    SqlMetadataExtractor,
    compute_ae_output_fields,
)


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
        assert "workflow_args" not in FetchProceduresInput.model_fields

    def test_fetch_procedures_output_has_counts(self) -> None:
        assert "chunk_count" in FetchProceduresOutput.model_fields
        assert "total_record_count" in FetchProceduresOutput.model_fields


class TestTypedTaskInputs:
    """Tests that per-task inputs use typed fields, not workflow_args dicts."""

    def test_fetch_databases_input_no_workflow_args(self) -> None:
        assert "workflow_args" not in FetchDatabasesInput.model_fields

    def test_fetch_schemas_input_no_workflow_args(self) -> None:
        assert "workflow_args" not in FetchSchemasInput.model_fields

    def test_fetch_tables_input_no_workflow_args(self) -> None:
        assert "workflow_args" not in FetchTablesInput.model_fields

    def test_fetch_columns_input_no_workflow_args(self) -> None:
        assert "workflow_args" not in FetchColumnsInput.model_fields

    def test_task_inputs_inherit_extraction_task_input(self) -> None:
        assert issubclass(FetchDatabasesInput, ExtractionTaskInput)
        assert issubclass(FetchSchemasInput, ExtractionTaskInput)
        assert issubclass(FetchTablesInput, ExtractionTaskInput)
        assert issubclass(FetchColumnsInput, ExtractionTaskInput)
        assert issubclass(FetchProceduresInput, ExtractionTaskInput)

    def test_extraction_task_input_has_typed_fields(self) -> None:
        field_names = set(ExtractionTaskInput.model_fields)
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


class TestExtractionOutputAEFields:
    """Tests for the AE/publish-app fields on ExtractionOutput."""

    def test_new_fields_default_to_empty_string(self) -> None:
        out = ExtractionOutput()
        assert out.transformed_data_prefix == ""
        assert out.connection_qualified_name == ""
        assert out.publish_state_prefix == ""
        assert out.current_state_prefix == ""

    def test_fields_accept_values(self) -> None:
        out = ExtractionOutput(
            transformed_data_prefix="artifacts/run/transformed",
            connection_qualified_name="default/trino/1234",
            publish_state_prefix="persistent-artifacts/apps/atlan-publish-app/state/default/trino/1234/publish-state",
            current_state_prefix="argo-artifacts/default/trino/1234/current-state",
        )
        assert out.transformed_data_prefix == "artifacts/run/transformed"
        assert out.connection_qualified_name == "default/trino/1234"
        assert (
            out.publish_state_prefix
            == "persistent-artifacts/apps/atlan-publish-app/state/default/trino/1234/publish-state"
        )
        assert (
            out.current_state_prefix
            == "argo-artifacts/default/trino/1234/current-state"
        )

    def test_serialization_includes_ae_fields(self) -> None:
        out = ExtractionOutput(
            transformed_data_prefix="artifacts/run/transformed",
            connection_qualified_name="default/trino/1234",
        )
        data = out.model_dump()
        assert data["transformed_data_prefix"] == "artifacts/run/transformed"
        assert data["connection_qualified_name"] == "default/trino/1234"

    def test_evolution_safety_old_payload_without_new_fields(self) -> None:
        """Old serialized ExtractionOutput without AE fields still deserializes."""
        old_payload = {"workflow_id": "wf-1", "success": True}
        out = ExtractionOutput.model_validate(old_payload)
        assert out.workflow_id == "wf-1"
        assert out.success is True
        assert out.transformed_data_prefix == ""
        assert out.connection_qualified_name == ""
        assert out.publish_state_prefix == ""
        assert out.current_state_prefix == ""

    def test_serialization_roundtrip(self) -> None:
        original = ExtractionOutput(
            workflow_id="wf-1",
            success=True,
            transformed_data_prefix="run/transformed",
            connection_qualified_name="default/snowflake/123",
            publish_state_prefix="persistent-artifacts/apps/atlan-publish-app/state/default/snowflake/123/publish-state",
            current_state_prefix="argo-artifacts/default/snowflake/123/current-state",
        )
        serialized = original.model_dump_json()
        restored = ExtractionOutput.model_validate_json(serialized)
        assert restored == original


class TestComputeAEOutputFields:
    """Tests for the compute_ae_output_fields() helper."""

    def test_strips_prefix_from_output_path(self) -> None:
        result = compute_ae_output_fields(
            output_path="./local/tmp/2024/01/01/run-1",
            output_prefix="./local/tmp/",
            connection_qualified_name="default/snowflake/123",
        )
        assert result["transformed_data_prefix"] == "2024/01/01/run-1/transformed"

    def test_no_prefix_match_uses_full_path(self) -> None:
        result = compute_ae_output_fields(
            output_path="artifacts/run-1",
            output_prefix="./local/tmp/",
            connection_qualified_name="default/pg/456",
        )
        assert result["transformed_data_prefix"] == "artifacts/run-1/transformed"

    def test_empty_prefix_uses_full_path(self) -> None:
        result = compute_ae_output_fields(
            output_path="some/path",
            output_prefix="",
            connection_qualified_name="default/bq/789",
        )
        assert result["transformed_data_prefix"] == "some/path/transformed"

    def test_empty_output_path_yields_empty_transformed_prefix(self) -> None:
        result = compute_ae_output_fields(
            output_path="",
            output_prefix="./local/tmp/",
            connection_qualified_name="default/snowflake/123",
        )
        assert result["transformed_data_prefix"] == ""

    def test_both_empty_yields_all_empty(self) -> None:
        result = compute_ae_output_fields(
            output_path="",
            output_prefix="",
            connection_qualified_name="",
        )
        assert result["transformed_data_prefix"] == ""
        assert result["connection_qualified_name"] == ""
        assert result["publish_state_prefix"] == ""
        assert result["current_state_prefix"] == ""

    def test_publish_state_prefix_format(self) -> None:
        result = compute_ae_output_fields(
            output_path="p",
            output_prefix="",
            connection_qualified_name="default/snowflake/abc",
        )
        assert result["publish_state_prefix"] == (
            "persistent-artifacts/apps/atlan-publish-app/state/default/snowflake/abc/publish-state"
        )

    def test_current_state_prefix_format(self) -> None:
        result = compute_ae_output_fields(
            output_path="p",
            output_prefix="",
            connection_qualified_name="default/snowflake/abc",
        )
        assert result["current_state_prefix"] == (
            "argo-artifacts/default/snowflake/abc/current-state"
        )

    def test_empty_connection_yields_empty_state_prefixes(self) -> None:
        """v3 returns empty strings when connection_qn is empty (diverges from v2)."""

        result = compute_ae_output_fields(
            output_path="./local/tmp/run",
            output_prefix="./local/tmp/",
            connection_qualified_name="",
        )
        assert result["connection_qualified_name"] == ""
        assert result["publish_state_prefix"] == ""
        assert result["current_state_prefix"] == ""

    def test_path_traversal_in_output_path_yields_empty(self) -> None:
        result = compute_ae_output_fields(
            output_path="../../etc/passwd",
            output_prefix="",
            connection_qualified_name="default/snowflake/123",
        )
        assert result["transformed_data_prefix"] == ""

    def test_path_traversal_after_prefix_strip_yields_empty(self) -> None:
        result = compute_ae_output_fields(
            output_path="artifacts/../../../sensitive",
            output_prefix="artifacts",
            connection_qualified_name="default/snowflake/123",
        )
        assert result["transformed_data_prefix"] == ""

    def test_unsafe_connection_qn_cleared(self) -> None:
        result = compute_ae_output_fields(
            output_path="some/path",
            output_prefix="",
            connection_qualified_name="../../other-tenant/x",
        )
        assert result["connection_qualified_name"] == ""
        assert result["publish_state_prefix"] == ""
        assert result["current_state_prefix"] == ""

    def test_absolute_output_path_yields_empty(self) -> None:
        result = compute_ae_output_fields(
            output_path="/etc/passwd",
            output_prefix="",
            connection_qualified_name="default/snowflake/123",
        )
        assert result["transformed_data_prefix"] == ""

    def test_trailing_slash_stripped_from_prefix(self) -> None:
        result = compute_ae_output_fields(
            output_path="./local/tmp/run/",
            output_prefix="./local/tmp/",
            connection_qualified_name="c",
        )
        assert result["transformed_data_prefix"] == "run/transformed"
