"""Unit tests for SqlMetadataExtractor template."""

from __future__ import annotations

from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError

import application_sdk.templates.sql_metadata_extractor as mod
from application_sdk.app.base import App
from application_sdk.app.task import get_task_metadata, is_task
from application_sdk.contracts.base import Output, PublishInputMixin
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
    FetchSchemasOutput,
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

        original_run = getattr(
            SqlMetadataExtractor, "_original_run", SqlMetadataExtractor.run
        )
        hints = get_type_hints(original_run)
        assert hints.get("input") is ExtractionInput

    def test_run_returns_extraction_output(self) -> None:
        original_run = getattr(
            SqlMetadataExtractor, "_original_run", SqlMetadataExtractor.run
        )
        hints = get_type_hints(original_run)
        assert hints.get("return") is ExtractionOutput

    def test_fetch_databases_input_type(self) -> None:
        meta = get_task_metadata(SqlMetadataExtractor.fetch_databases)
        assert meta.input_type is FetchDatabasesInput

    def test_fetch_databases_output_type(self) -> None:
        meta = get_task_metadata(SqlMetadataExtractor.fetch_databases)
        assert meta.output_type is FetchDatabasesOutput

    def test_fetch_databases_timeout(self) -> None:
        meta = get_task_metadata(SqlMetadataExtractor.fetch_databases)
        assert meta.timeout_seconds == 1800

    def test_fetch_procedures_input_type(self) -> None:
        meta = get_task_metadata(SqlMetadataExtractor.fetch_procedures)
        assert meta.input_type is FetchProceduresInput

    def test_fetch_procedures_output_type(self) -> None:
        meta = get_task_metadata(SqlMetadataExtractor.fetch_procedures)
        assert meta.output_type is FetchProceduresOutput


class TestFetchProceduresTask:
    """Tests for the fetch_procedures task on SqlMetadataExtractor."""

    def test_fetch_procedures_is_task_decorated(self) -> None:
        assert is_task(SqlMetadataExtractor.fetch_procedures)

    async def test_fetch_procedures_raises_not_implemented(self) -> None:
        extractor = SqlMetadataExtractor.__new__(SqlMetadataExtractor)
        with pytest.raises(NotImplementedError):
            await extractor.fetch_procedures(FetchProceduresInput())

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


class TestExtractionInputAgentJsonValidation:
    """Tests for _skip_agent_json_for_direct model validator."""

    def test_direct_mode_nulls_invalid_agent_json(self) -> None:
        """agent_json with placeholder values (port='port') is accepted in direct mode."""
        import json

        inp = ExtractionInput.model_validate(
            {
                "extraction_method": "direct",
                "agent_json": json.dumps(
                    {"host": "host", "port": "port", "agent-name": "test"}
                ),
                "credential_guid": "test-guid",
            }
        )
        assert inp.agent_json is None
        assert inp.credential_guid == "test-guid"

    def test_agent_mode_validates_agent_json(self) -> None:
        """agent_json is validated normally when extraction_method is agent."""
        import json

        inp = ExtractionInput.model_validate(
            {
                "extraction_method": "agent",
                "agent_json": json.dumps(
                    {
                        "host": "db.example.com",
                        "port": 5432,
                        "agent-name": "my-agent",
                        "secret-manager": "awssecretmanager",
                        "auth-type": "basic",
                    }
                ),
            }
        )
        assert inp.agent_json is not None
        assert inp.agent_json.port == 5432

    def test_empty_extraction_method_nulls_agent_json(self) -> None:
        """Empty extraction_method (defaults to direct) also nulls agent_json."""
        inp = ExtractionInput.model_validate(
            {
                "extraction_method": "",
                "agent_json": '{"host": "host", "port": "port"}',
                "credential_guid": "test-guid",
            }
        )
        assert inp.agent_json is None

    def test_no_agent_json_still_works(self) -> None:
        """ExtractionInput without agent_json works for both direct and agent."""
        inp = ExtractionInput.model_validate(
            {"extraction_method": "direct", "credential_guid": "test-guid"}
        )
        assert inp.agent_json is None


class TestSqlMetadataExtractorSubclass:
    """Tests for subclassing SqlMetadataExtractor."""

    def test_sql_metadata_extractor_is_registered_at_import(self) -> None:
        # SqlMetadataExtractor registers itself at import time;
        # the conftest reset clears it — so we verify the class ITSELF is an App subclass
        # and that its structure is correct (covered by TestSqlMetadataExtractorStructure).
        # Direct subclasses inherit _app_registered=True so they don't auto-register.
        assert issubclass(SqlMetadataExtractor, App)

    async def test_fetch_databases_raises_not_implemented_by_default(self) -> None:
        extractor = SqlMetadataExtractor.__new__(SqlMetadataExtractor)
        with pytest.raises(NotImplementedError):
            await extractor.fetch_databases(FetchDatabasesInput())


class _StubSQLClient:
    """Test double for BaseSQLClient.

    Exposes ``last_query`` and ``closed`` so tests can assert the
    extractor prepared the right SQL and cleaned up after itself.
    """

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self._rows = rows or []
        self.loaded_with: dict[str, object] | None = None
        self.last_query: str | None = None
        self.closed: bool = False

    async def load(self, credentials: dict[str, object]) -> None:
        self.loaded_with = credentials

    async def run_query(self, query: str, batch_size: int = 100000):
        self.last_query = query
        # Single-batch generator — matches BaseSQLClient's async iterator.
        yield self._rows

    async def close(self) -> None:
        self.closed = True


class TestSqlMetadataExtractorPrepareSql:
    """Tests for the ``_prepare_sql`` filter placeholder substitution."""

    def _extractor(self) -> SqlMetadataExtractor:
        return SqlMetadataExtractor.__new__(SqlMetadataExtractor)

    def test_substitutes_default_regexes_when_filters_empty(self) -> None:
        sql = (
            "WHERE name !~ '{normalized_exclude_regex}' "
            "AND name ~ '{normalized_include_regex}'"
        )
        result = self._extractor()._prepare_sql(sql, ExtractionTaskInput())
        assert result == "WHERE name !~ '^$' AND name ~ '.*'"

    def test_substitutes_user_filters(self) -> None:
        sql = "{normalized_exclude_regex}|{normalized_include_regex}"
        result = self._extractor()._prepare_sql(
            sql,
            ExtractionTaskInput(exclude_filter="^tmp_", include_filter="^prod_"),
        )
        assert result == "^tmp_|^prod_"

    def test_temp_table_fragment_injected_in_table_mode(self) -> None:
        class _E(SqlMetadataExtractor):
            _app_registered = True
            extract_temp_table_regex_table_sql = "AND name !~ '{exclude_table_regex}'"

        extractor = _E.__new__(_E)
        result = extractor._prepare_sql(
            "{temp_table_regex_sql}",
            ExtractionTaskInput(temp_table_regex="tmp_.*"),
        )
        assert result == "AND name !~ 'tmp_.*'"

    def test_temp_table_fragment_uses_column_variant_in_column_mode(self) -> None:
        class _E(SqlMetadataExtractor):
            _app_registered = True
            extract_temp_table_regex_table_sql = "TABLE({exclude_table_regex})"
            extract_temp_table_regex_column_sql = "COLUMN({exclude_table_regex})"

        extractor = _E.__new__(_E)
        table_sql = extractor._prepare_sql(
            "{temp_table_regex_sql}",
            ExtractionTaskInput(temp_table_regex="tmp_.*"),
            column_mode=False,
        )
        column_sql = extractor._prepare_sql(
            "{temp_table_regex_sql}",
            ExtractionTaskInput(temp_table_regex="tmp_.*"),
            column_mode=True,
        )
        assert table_sql == "TABLE(tmp_.*)"
        assert column_sql == "COLUMN(tmp_.*)"

    def test_temp_table_placeholder_empty_when_no_regex(self) -> None:
        class _E(SqlMetadataExtractor):
            _app_registered = True
            extract_temp_table_regex_table_sql = "AND name !~ '{exclude_table_regex}'"

        extractor = _E.__new__(_E)
        result = extractor._prepare_sql(
            "x {temp_table_regex_sql} y", ExtractionTaskInput()
        )
        assert result == "x  y"

    def test_temp_table_placeholder_empty_when_fragment_unset(self) -> None:
        extractor = self._extractor()
        result = extractor._prepare_sql(
            "x {temp_table_regex_sql} y",
            ExtractionTaskInput(temp_table_regex="tmp_.*"),
        )
        assert result == "x  y"


class TestSqlMetadataExtractorLoadSqlClient:
    """Tests for ``_load_sql_client`` and default fetch task execution."""

    async def test_load_sql_client_raises_when_class_not_set(self) -> None:
        extractor = SqlMetadataExtractor.__new__(SqlMetadataExtractor)
        with pytest.raises(NotImplementedError, match="sql_client_class"):
            await extractor._load_sql_client(ExtractionTaskInput())

    async def test_load_sql_client_instantiates_and_loads(self) -> None:
        created: list[_StubSQLClient] = []

        class _Stub(_StubSQLClient):
            def __init__(self) -> None:
                super().__init__()
                created.append(self)

        class _E(SqlMetadataExtractor):
            _app_registered = True
            sql_client_class = _Stub  # type: ignore[assignment]

        extractor = _E.__new__(_E)

        async def _fake_get_credentials(_input: ExtractionTaskInput) -> dict[str, Any]:
            return {"user": "u", "pass": "p"}

        extractor._get_credentials = _fake_get_credentials  # type: ignore[method-assign]

        client = await extractor._load_sql_client(ExtractionTaskInput())

        assert isinstance(client, _Stub)
        assert client.loaded_with == {"user": "u", "pass": "p"}
        assert created == [client]

    async def test_fetch_databases_happy_path(self) -> None:
        rows = [
            {"database_name": "db1"},
            {"database_name": "db2"},
            {"database_name": ""},  # filtered out
        ]
        stub = _StubSQLClient(rows=rows)

        class _E(SqlMetadataExtractor):
            _app_registered = True
            sql_client_class = type(stub)  # type: ignore[assignment]
            fetch_database_sql = (
                "SELECT db FROM meta "
                "WHERE db !~ '{normalized_exclude_regex}' "
                "AND db ~ '{normalized_include_regex}'"
            )

        extractor = _E.__new__(_E)

        async def _fake_load(_input: ExtractionTaskInput):
            return stub

        extractor._load_sql_client = _fake_load  # type: ignore[method-assign]

        out = await extractor.fetch_databases(
            FetchDatabasesInput(exclude_filter="^x$", include_filter="^prod_"),
        )

        assert isinstance(out, FetchDatabasesOutput)
        assert out.databases == ["db1", "db2"]
        assert out.total_record_count == 2
        assert out.chunk_count == 1
        assert stub.last_query is not None
        assert "^x$" in stub.last_query
        assert "^prod_" in stub.last_query
        assert stub.closed is True

    async def test_fetch_schemas_happy_path(self) -> None:
        rows = [
            {"schema_name": "public"},
            {"schema_name": "analytics"},
            {"schema_name": ""},  # filtered out
        ]
        stub = _StubSQLClient(rows=rows)

        class _E(SqlMetadataExtractor):
            _app_registered = True
            sql_client_class = type(stub)  # type: ignore[assignment]
            fetch_schema_sql = (
                "SELECT schema FROM meta "
                "WHERE schema !~ '{normalized_exclude_regex}' "
                "AND schema ~ '{normalized_include_regex}'"
            )

        extractor = _E.__new__(_E)

        async def _fake_load(_input: ExtractionTaskInput):
            return stub

        extractor._load_sql_client = _fake_load  # type: ignore[method-assign]

        out = await extractor.fetch_schemas(
            FetchSchemasInput(exclude_filter="^tmp_", include_filter=".*"),
        )

        assert isinstance(out, FetchSchemasOutput)
        assert out.schemas == ["public", "analytics"]
        assert out.total_record_count == 2
        assert out.chunk_count == 1
        assert stub.last_query is not None
        assert "^tmp_" in stub.last_query
        assert stub.closed is True

    async def test_fetch_columns_streams_and_counts(self) -> None:
        # Three batches — extractor must sum len() across them, not materialize rows.
        batches = [
            [{"c": 1}, {"c": 2}],
            [{"c": 3}],
            [{"c": 4}, {"c": 5}, {"c": 6}],
        ]

        class _MultiBatchClient(_StubSQLClient):
            async def run_query(self, query: str, batch_size: int = 100000):
                self.last_query = query
                for batch in batches:
                    yield batch

        stub = _MultiBatchClient()

        class _E(SqlMetadataExtractor):
            _app_registered = True
            sql_client_class = type(stub)  # type: ignore[assignment]
            fetch_column_sql = "SELECT * FROM columns"

        extractor = _E.__new__(_E)

        async def _fake_load(_input: ExtractionTaskInput):
            return stub

        extractor._load_sql_client = _fake_load  # type: ignore[method-assign]

        out = await extractor.fetch_columns(FetchColumnsInput())

        assert out.total_record_count == 6
        assert out.chunk_count == 1
        assert stub.closed is True

    async def test_fetch_tables_closes_client_on_exception(self) -> None:
        class _BoomClient(_StubSQLClient):
            async def run_query(self, query: str, batch_size: int = 100000):
                self.last_query = query
                raise RuntimeError("boom")
                yield  # pragma: no cover — satisfy async-generator typing

        stub = _BoomClient()

        class _E(SqlMetadataExtractor):
            _app_registered = True
            sql_client_class = type(stub)  # type: ignore[assignment]
            fetch_table_sql = "SELECT t FROM meta"

        extractor = _E.__new__(_E)

        async def _fake_load(_input: ExtractionTaskInput):
            return stub

        extractor._load_sql_client = _fake_load  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="boom"):
            await extractor.fetch_tables(FetchTablesInput())

        assert stub.closed is True


class TestPublishInputMixin:
    """Tests for PublishInputMixin mixin — auto-derives state prefixes."""

    def test_auto_derives_state_prefixes(self) -> None:
        out = PublishInputMixin(connection_qualified_name="default/snowflake/123")
        assert "default/snowflake/123" in out.publish_state_prefix
        assert "default/snowflake/123" in out.current_state_prefix

    def test_empty_connection_yields_empty_prefixes(self) -> None:
        out = PublishInputMixin()
        assert out.publish_state_prefix == ""
        assert out.current_state_prefix == ""

    def test_explicit_values_not_overridden(self) -> None:
        out = PublishInputMixin(
            connection_qualified_name="default/pg/456",
            publish_state_prefix="custom/publish",
            current_state_prefix="custom/current",
        )
        assert out.publish_state_prefix == "custom/publish"
        assert out.current_state_prefix == "custom/current"

    def test_unsafe_connection_qn_no_derivation(self) -> None:
        out = PublishInputMixin(connection_qualified_name="../../attack")
        assert out.publish_state_prefix == ""
        assert out.current_state_prefix == ""

    def test_used_as_mixin(self) -> None:
        """Apps use PublishInputMixin as mixin alongside Output."""

        class MyOutput(Output, PublishInputMixin, allow_unbounded_fields=True):
            records: int = 0

        out = MyOutput(
            records=100,
            connection_qualified_name="default/trino/789",
            transformed_data_prefix="artifacts/transformed",
        )
        assert out.records == 100
        assert out.transformed_data_prefix == "artifacts/transformed"
        assert "default/trino/789" in out.publish_state_prefix

    def test_output_path_derives_transformed_prefix(self) -> None:
        """output_path + output_prefix auto-derives transformed_data_prefix."""

        out = PublishInputMixin(
            connection_qualified_name="default/snowflake/123",
            output_path="./local/tmp/artifacts/apps/my-app/workflows/wf-1/run-1",
            output_prefix="./local/tmp/",
        )
        assert (
            out.transformed_data_prefix
            == "artifacts/apps/my-app/workflows/wf-1/run-1/transformed"
        )

    def test_output_path_no_prefix_uses_full(self) -> None:
        out = PublishInputMixin(
            connection_qualified_name="c",
            output_path="some/path",
        )
        assert out.transformed_data_prefix == "some/path/transformed"

    def test_output_path_auto_resolve_outside_temporal(self) -> None:
        """Outside Temporal context, output_path stays empty — no error."""

        out = PublishInputMixin(
            connection_qualified_name="default/snowflake/123",
        )
        # Auto-resolve fails gracefully outside Temporal
        assert out.publish_state_prefix != ""
        assert out.current_state_prefix != ""
        # transformed_data_prefix empty since output_path couldn't be resolved
        assert out.transformed_data_prefix == ""

    def test_explicit_transformed_prefix_not_overridden(self) -> None:
        out = PublishInputMixin(
            connection_qualified_name="c",
            output_path="some/path",
            transformed_data_prefix="custom/transformed",
        )
        assert out.transformed_data_prefix == "custom/transformed"

    def test_path_traversal_in_output_path_yields_empty(self) -> None:
        out = PublishInputMixin(
            connection_qualified_name="c",
            output_path="../../etc/passwd",
        )
        assert out.transformed_data_prefix == ""


class TestFilterFieldValidation:
    """Negative tests for the _SAFE_FILTER_PATTERN SQL-injection guard.

    _prepare_sql() substitutes exclude_filter / include_filter / temp_table_regex
    directly into SQL string literals via str.replace(). The Pydantic pattern
    constraint (^[^']*$) is the primary guard; these tests pin that invariant.
    """

    def test_exclude_filter_rejects_single_quote(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionTaskInput(exclude_filter="prefix'injection")

    def test_include_filter_rejects_single_quote(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionTaskInput(include_filter=".*'.*")

    def test_temp_table_regex_rejects_single_quote(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionTaskInput(temp_table_regex="tmp_'suffix")

    def test_safe_values_accepted(self) -> None:
        inp = ExtractionTaskInput(
            exclude_filter="^tmp_.*$",
            include_filter="^prod_.*$",
            temp_table_regex="^temp_.*$",
        )
        assert inp.exclude_filter == "^tmp_.*$"
        assert inp.include_filter == "^prod_.*$"
        assert inp.temp_table_regex == "^temp_.*$"

    def test_empty_values_accepted(self) -> None:
        inp = ExtractionTaskInput()
        assert inp.exclude_filter == ""
        assert inp.include_filter == ""
        assert inp.temp_table_regex == ""


class TestGetCredentials:
    """Tests for _get_credentials() error paths."""

    async def test_raises_when_no_credential_ref_and_no_guid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "get_infrastructure", lambda: None)
        extractor = SqlMetadataExtractor.__new__(SqlMetadataExtractor)
        with pytest.raises(
            ValueError, match="No credential reference or GUID available"
        ):
            await extractor._get_credentials(ExtractionTaskInput())

    async def test_raises_when_no_secret_store_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "get_infrastructure", lambda: None)
        extractor = SqlMetadataExtractor.__new__(SqlMetadataExtractor)
        with pytest.raises(ValueError, match="No secret store available"):
            await extractor._get_credentials(
                ExtractionTaskInput(credential_guid="some-guid-that-needs-a-store")
            )


class TestConnectionQnExtraction:
    """Tests for connection_qualified_name extraction logic in run().

    The run() method extracts connection_qualified_name from input.connection
    using this inline pattern:
        connection_qn = ""
        if input.connection and input.connection.attributes:
            connection_qn = input.connection.attributes.qualified_name or ""
    These tests verify the ExtractionInput → connection_qn derivation.
    """

    @staticmethod
    def _extract_connection_qn(inp: ExtractionInput) -> str:
        """Replicate the inline connection_qn extraction from run()."""
        connection_qn = ""
        if inp.connection and inp.connection.attributes:
            connection_qn = inp.connection.attributes.qualified_name or ""
        return connection_qn

    def test_returns_qualified_name_from_connection(self) -> None:
        from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef

        inp = ExtractionInput(
            connection=ConnectionRef(
                attributes=ConnectionAttributes(
                    qualified_name="default/alloydb-postgres/my-conn"
                )
            )
        )
        assert self._extract_connection_qn(inp) == "default/alloydb-postgres/my-conn"

    def test_returns_empty_when_no_connection(self) -> None:
        inp = ExtractionInput()
        assert self._extract_connection_qn(inp) == ""

    def test_returns_empty_when_no_attributes(self) -> None:
        from application_sdk.contracts.types import ConnectionRef

        inp = ExtractionInput(connection=ConnectionRef())
        assert self._extract_connection_qn(inp) == ""

    def test_returns_empty_when_qualified_name_is_empty(self) -> None:
        from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef

        inp = ExtractionInput(
            connection=ConnectionRef(attributes=ConnectionAttributes(qualified_name=""))
        )
        assert self._extract_connection_qn(inp) == ""


class TestExtractionOutputFields:
    """Tests for connection_qualified_name, output_path, output_prefix on ExtractionOutput.

    The fix ensures run() passes these fields to ExtractionOutput so that
    PublishInputMixin can auto-derive publish_state_prefix and current_state_prefix.
    """

    def test_sets_connection_qualified_name(self) -> None:
        output = ExtractionOutput(
            success=True,
            connection_qualified_name="default/postgres/prod",
            databases_extracted=2,
            schemas_extracted=5,
            records_uploaded=10,
        )
        assert output.connection_qualified_name == "default/postgres/prod"

    def test_sets_output_path_and_prefix(self) -> None:
        output = ExtractionOutput(
            success=True,
            output_path="artifacts/apps/my-app/workflows/wf1/run1",
            output_prefix="/tmp",
        )
        assert output.output_path == "artifacts/apps/my-app/workflows/wf1/run1"
        assert output.output_prefix == "/tmp"

    def test_defaults_to_empty_strings(self) -> None:
        output = ExtractionOutput()
        assert output.connection_qualified_name == ""
        assert output.output_path == ""
        assert output.output_prefix == ""

    def test_publish_state_prefix_derived_from_connection_qn(self) -> None:
        output = ExtractionOutput(
            connection_qualified_name="default/postgres/prod",
        )
        assert "default/postgres/prod" in output.publish_state_prefix
        assert "default/postgres/prod" in output.current_state_prefix
