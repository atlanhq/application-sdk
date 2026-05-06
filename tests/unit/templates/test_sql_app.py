"""Unit tests for SqlApp consolidated SQL template (BLDX-968)."""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    FetchColumnsInput,
    FetchDatabasesInput,
    FetchProceduresInput,
    FetchSchemasInput,
    FetchTablesInput,
    FetchViewsInput,
    TransformInput,
)
from application_sdk.templates.sql_app import SqlApp

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class FakeSQLClient:
    """Mock SQL client for testing."""

    def __init__(self, results: pd.DataFrame | None = None):
        self.loaded = False
        self._results = results if results is not None else pd.DataFrame()

    async def load(self, credentials=None):
        self.loaded = True

    async def close(self):
        pass

    async def get_results(self, sql: str) -> pd.DataFrame:
        return self._results


def _mock_init_client(results: pd.DataFrame) -> AsyncMock:
    """Create an AsyncMock that returns a FakeSQLClient with given results."""
    return AsyncMock(return_value=FakeSQLClient(results=results))


class TestSqlApp(SqlApp):
    """Concrete SqlApp for testing with fake SQL and mappers."""

    sql_client_class: ClassVar = FakeSQLClient  # type: ignore[assignment]
    _app_registered: ClassVar[bool] = True

    fetch_database_sql: ClassVar[str] = "SELECT db_name as database_name FROM databases"
    fetch_schema_sql: ClassVar[str] = (
        "SELECT schema_name FROM schemas WHERE db = '{normalized_include_regex}'"
    )
    fetch_table_sql: ClassVar[str] = (
        "SELECT table_name FROM tables {temp_table_regex_sql}"
    )
    fetch_column_sql: ClassVar[str] = "SELECT column_name FROM columns"

    def map_database(self, record: dict[str, Any], connection_qn: str) -> dict:
        return {
            "typeName": "Database",
            "qualifiedName": f"{connection_qn}/{record.get('database_name', '')}",
        }

    def map_schema(self, record: dict[str, Any], connection_qn: str) -> dict:
        return {
            "typeName": "Schema",
            "qualifiedName": f"{connection_qn}/{record.get('schema_name', '')}",
        }

    def map_table(self, record: dict[str, Any], connection_qn: str) -> dict:
        return {
            "typeName": "Table",
            "qualifiedName": f"{connection_qn}/{record.get('table_name', '')}",
        }

    def map_column(self, record: dict[str, Any], connection_qn: str) -> dict:
        return {
            "typeName": "Column",
            "qualifiedName": f"{connection_qn}/{record.get('column_name', '')}",
        }


@pytest.fixture
def app():
    return TestSqlApp()


def _make_task_input(cls, output_path="/tmp/test", **kwargs):
    """Helper to create task inputs for testing."""
    defaults = {
        "workflow_id": "test-wf",
        "output_path": output_path,
        "output_prefix": "/tmp",
        "exclude_filter": "",
        "include_filter": "",
        "temp_table_regex": "",
    }
    defaults.update(kwargs)
    return cls(**defaults)


# ---------------------------------------------------------------------------
# build_task_input (BLDX-1138)
# ---------------------------------------------------------------------------


class TestBuildTaskInput:
    """BLDX-1138: build_task_input as public API."""

    def test_builds_fetch_databases_input(self):
        src = ExtractionInput(
            workflow_id="wf-1",
            output_path="/out",
            output_prefix="/pfx",
            exclude_filter="^temp$",
            include_filter="^prod$",
            temp_table_regex="^tmp_",
        )
        result = SqlApp.build_task_input(FetchDatabasesInput, src)
        assert result.workflow_id == "wf-1"
        assert result.output_path == "/out"
        assert result.exclude_filter == "^temp$"
        assert result.include_filter == "^prod$"

    def test_builds_with_credential_ref(self):
        src = ExtractionInput(workflow_id="wf-2")
        cred_ref = CredentialRef(credential_guid="test-guid")
        result = SqlApp.build_task_input(FetchSchemasInput, src, cred_ref=cred_ref)
        assert result.credential_ref.credential_guid == "test-guid"

    def test_builds_different_input_types(self):
        src = ExtractionInput(workflow_id="wf-3")
        for cls in [
            FetchDatabasesInput,
            FetchSchemasInput,
            FetchTablesInput,
            FetchColumnsInput,
        ]:
            result = SqlApp.build_task_input(cls, src)
            assert result.workflow_id == "wf-3"


# ---------------------------------------------------------------------------
# fetch_* tasks
# ---------------------------------------------------------------------------


class TestFetchTasks:
    """Test SQL metadata fetch tasks."""

    async def test_fetch_databases_returns_count(self, app, tmp_path):
        df = pd.DataFrame({"database_name": ["db1", "db2"]})
        input_ = _make_task_input(FetchDatabasesInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(df)):
            result = await app.fetch_databases(input_)

        assert result.total_record_count == 2
        assert result.chunk_count == 1

    async def test_fetch_databases_empty_sql_returns_zero(self, app):
        app.fetch_database_sql = ""
        input_ = _make_task_input(FetchDatabasesInput)
        result = await app.fetch_databases(input_)
        assert result.total_record_count == 0
        assert result.chunk_count == 0

    async def test_fetch_schemas_writes_parquet(self, app, tmp_path):
        df = pd.DataFrame({"schema_name": ["public", "private"]})
        input_ = _make_task_input(FetchSchemasInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(df)):
            result = await app.fetch_schemas(input_)

        assert result.total_record_count == 2
        parquet_dir = tmp_path / "raw" / "schema"
        assert parquet_dir.exists()
        assert len(list(parquet_dir.glob("*.parquet"))) == 1

    async def test_fetch_tables_returns_count(self, app, tmp_path):
        df = pd.DataFrame({"table_name": ["users", "orders", "products"]})
        input_ = _make_task_input(FetchTablesInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(df)):
            result = await app.fetch_tables(input_)

        assert result.total_record_count == 3

    async def test_fetch_columns_returns_count(self, app, tmp_path):
        df = pd.DataFrame({"column_name": ["id", "name", "email", "created_at"]})
        input_ = _make_task_input(FetchColumnsInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(df)):
            result = await app.fetch_columns(input_)

        assert result.total_record_count == 4


# ---------------------------------------------------------------------------
# fetch_views / fetch_procedures stubs (BLDX-1139)
# ---------------------------------------------------------------------------


class TestFetchViewsAndProcedures:
    """BLDX-1139: fetch_views and fetch_procedures stubs."""

    async def test_fetch_views_returns_zero_when_no_sql(self, app):
        input_ = _make_task_input(FetchViewsInput)
        result = await app.fetch_views(input_)
        assert result.total_record_count == 0

    async def test_fetch_views_with_sql_returns_count(self, app, tmp_path):
        app.fetch_view_sql = "SELECT view_name FROM views"
        df = pd.DataFrame({"view_name": ["v1", "v2"]})
        input_ = _make_task_input(FetchViewsInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(df)):
            result = await app.fetch_views(input_)

        assert result.total_record_count == 2

    async def test_fetch_procedures_returns_zero_when_no_sql(self, app):
        input_ = _make_task_input(FetchProceduresInput)
        result = await app.fetch_procedures(input_)
        assert result.total_record_count == 0


# ---------------------------------------------------------------------------
# Per-entity transforms (BLDX-1140)
# ---------------------------------------------------------------------------


class TestPerEntityTransforms:
    """BLDX-1140: per-entity transform tasks using asset mapper."""

    async def test_transform_tables_uses_mapper(self, app, tmp_path):
        # Write raw parquet
        raw_dir = tmp_path / "raw" / "table"
        raw_dir.mkdir(parents=True)
        df = pd.DataFrame({"table_name": ["users", "orders"]})
        df.to_parquet(str(raw_dir / "chunk-0-part0.parquet"))

        input_ = _make_task_input(TransformInput, output_path=str(tmp_path))
        result = await app.transform_tables(input_)

        assert result.total_record_count == 2
        output_file = tmp_path / "transformed" / "table" / "entities.json"
        assert output_file.exists()

        lines = output_file.read_text().strip().split("\n")
        assert len(lines) == 2
        entity = json.loads(lines[0])
        assert entity["typeName"] == "Table"

    async def test_transform_columns_uses_mapper(self, app, tmp_path):
        raw_dir = tmp_path / "raw" / "column"
        raw_dir.mkdir(parents=True)
        df = pd.DataFrame({"column_name": ["id", "name", "email"]})
        df.to_parquet(str(raw_dir / "chunk-0-part0.parquet"))

        input_ = _make_task_input(TransformInput, output_path=str(tmp_path))
        result = await app.transform_columns(input_)

        assert result.total_record_count == 3

    async def test_transform_returns_zero_when_no_raw_data(self, app, tmp_path):
        input_ = _make_task_input(TransformInput, output_path=str(tmp_path))
        result = await app.transform_tables(input_)
        assert result.total_record_count == 0

    async def test_transform_data_raises_not_implemented(self, app):
        input_ = _make_task_input(TransformInput)
        with pytest.raises(NotImplementedError):
            await app.transform_data(input_)


# ---------------------------------------------------------------------------
# Asset mapper stubs
# ---------------------------------------------------------------------------


class TestAssetMapperStubs:
    """Asset mapper stubs raise NotImplementedError on base SqlApp."""

    def test_base_map_database_raises(self):
        base = SqlApp()
        with pytest.raises(NotImplementedError):
            base.map_database({}, "conn/qn")

    def test_base_map_schema_raises(self):
        base = SqlApp()
        with pytest.raises(NotImplementedError):
            base.map_schema({}, "conn/qn")

    def test_base_map_table_raises(self):
        base = SqlApp()
        with pytest.raises(NotImplementedError):
            base.map_table({}, "conn/qn")

    def test_base_map_column_raises(self):
        base = SqlApp()
        with pytest.raises(NotImplementedError):
            base.map_column({}, "conn/qn")

    def test_subclass_mappers_work(self, app):
        result = app.map_table({"table_name": "users"}, "default/mysql/1234")
        assert result["typeName"] == "Table"
        assert "users" in result["qualifiedName"]


# ---------------------------------------------------------------------------
# _prepare_sql
# ---------------------------------------------------------------------------


class TestPrepareSql:
    """Test SQL template substitution."""

    def test_substitutes_include_exclude_regex(self, app):
        sql = "SELECT * FROM t WHERE schema ~ '{normalized_include_regex}' AND schema !~ '{normalized_exclude_regex}'"
        input_ = _make_task_input(
            FetchTablesInput,
            include_filter="^prod$",
            exclude_filter="^temp$",
        )
        result = app._prepare_sql(sql, input_)
        assert "^prod$" in result
        assert "^temp$" in result

    def test_default_include_is_wildcard(self, app):
        sql = "WHERE schema ~ '{normalized_include_regex}'"
        input_ = _make_task_input(FetchTablesInput)
        result = app._prepare_sql(sql, input_)
        assert ".*" in result

    def test_default_exclude_is_nothing(self, app):
        sql = "WHERE schema !~ '{normalized_exclude_regex}'"
        input_ = _make_task_input(FetchTablesInput)
        result = app._prepare_sql(sql, input_)
        assert "^$" in result

    def test_temp_table_regex_substitution(self, app):
        app.extract_temp_table_regex_table_sql = "AND t.name !~ '{exclude_table_regex}'"
        sql = "SELECT * FROM t {temp_table_regex_sql}"
        input_ = _make_task_input(FetchTablesInput, temp_table_regex="^tmp_")
        result = app._prepare_sql(sql, input_)
        assert "AND t.name !~ '^tmp_'" in result

    def test_dict_filter_normalized_to_regex(self, app):
        sql = "WHERE schema ~ '{normalized_include_regex}'"
        input_ = _make_task_input(
            FetchTablesInput,
            include_filter={"^prod$": ["^public$"]},
        )
        result = app._prepare_sql(sql, input_)
        # normalize_filters should produce a regex string
        assert "{normalized_include_regex}" not in result


# ---------------------------------------------------------------------------
# Class hierarchy
# ---------------------------------------------------------------------------


class TestClassHierarchy:
    def test_sql_app_extends_app(self):
        from application_sdk.app.base import App

        assert issubclass(SqlApp, App)

    def test_sql_app_is_abstract(self):
        assert SqlApp._app_registered is True

    def test_import_from_templates(self):
        from application_sdk.templates import SqlApp as Imported

        assert Imported is SqlApp


# ---------------------------------------------------------------------------
# SqlApp.run() — transformed_data_prefix derivation
# ---------------------------------------------------------------------------


class TestRunOutputPrefixes:
    """Verify SqlApp.run() derives transformed_data_prefix from workflow context.

    The fix for https://github.com/atlanhq/atlan-mysql-app/issues/64:
    run() is a Temporal *workflow* method, not an activity — calling
    build_output_path() (which calls activity.info()) raised
    "Not in activity context". The fix uses workflow.info() instead.
    """

    def _make_minimal_app(self):
        app = SqlApp.__new__(SqlApp)
        app._app_name = "test-app"
        return app

    def test_uses_input_output_path_when_set(self):
        """When input.output_path is provided, use it directly (no workflow context needed)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.templates.contracts.sql_metadata import (
            ExtractionInput,
            ExtractionOutput,
        )

        app = self._make_minimal_app()
        mock_result = ExtractionOutput(
            databases_extracted=1,
            schemas_extracted=1,
            tables_extracted=2,
            columns_extracted=10,
            connection_qualified_name="default/mysql/123",
        )

        input_ = ExtractionInput(
            output_path="./local/tmp/artifacts/apps/test/workflows/wf-1/run-1"
        )

        with (
            patch.object(
                SqlApp,
                "fetch_databases",
                new=AsyncMock(return_value=MagicMock(total_record_count=1)),
            ),
            patch.object(
                SqlApp,
                "fetch_schemas",
                new=AsyncMock(return_value=MagicMock(total_record_count=1)),
            ),
            patch.object(
                SqlApp,
                "fetch_tables",
                new=AsyncMock(return_value=MagicMock(total_record_count=2)),
            ),
            patch.object(
                SqlApp,
                "fetch_columns",
                new=AsyncMock(return_value=MagicMock(total_record_count=10)),
            ),
            patch.object(
                SqlApp, "transform_databases", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_schemas", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_tables", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_columns", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "upload_to_atlan", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
        ):
            import asyncio

            result = asyncio.run(app.run(input_))

        # With output_path set, transformed_data_prefix uses it directly
        assert (
            "artifacts/apps/test/workflows/wf-1/run-1/transformed"
            in result.transformed_data_prefix
        )

    def test_uses_workflow_info_when_output_path_empty(self):
        """When input.output_path is empty, derive path from workflow.info()."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        app = self._make_minimal_app()

        # Simulate workflow.info() returning a known workflow_id/run_id
        mock_wf_info = MagicMock()
        mock_wf_info.workflow_id = "test-wf-123"
        mock_wf_info.run_id = "test-run-456"

        input_ = ExtractionInput(output_path="")  # empty — should use workflow context

        with (
            patch(
                "application_sdk.templates.sql_app._temporal_workflow.info",
                return_value=mock_wf_info,
            ),
            patch.object(
                SqlApp,
                "fetch_databases",
                new=AsyncMock(return_value=MagicMock(total_record_count=1)),
            ),
            patch.object(
                SqlApp,
                "fetch_schemas",
                new=AsyncMock(return_value=MagicMock(total_record_count=1)),
            ),
            patch.object(
                SqlApp,
                "fetch_tables",
                new=AsyncMock(return_value=MagicMock(total_record_count=2)),
            ),
            patch.object(
                SqlApp,
                "fetch_columns",
                new=AsyncMock(return_value=MagicMock(total_record_count=10)),
            ),
            patch.object(
                SqlApp, "transform_databases", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_schemas", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_tables", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_columns", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "upload_to_atlan", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
        ):
            import asyncio

            result = asyncio.run(app.run(input_))

        # Must contain the workflow_id and run_id from workflow.info()
        assert "test-wf-123" in result.transformed_data_prefix
        assert "test-run-456" in result.transformed_data_prefix
        assert result.transformed_data_prefix.endswith("/transformed")

    def test_build_output_path_not_called_in_run(self):
        """build_output_path() (activity-only) must NOT be called from run()."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        app = self._make_minimal_app()
        mock_wf_info = MagicMock()
        mock_wf_info.workflow_id = "wf-x"
        mock_wf_info.run_id = "run-x"

        input_ = ExtractionInput(output_path="")

        with (
            patch(
                "application_sdk.templates.sql_app._temporal_workflow.info",
                return_value=mock_wf_info,
            ),
            patch("application_sdk.templates.sql_app.build_output_path") as mock_bop,
            patch.object(
                SqlApp,
                "fetch_databases",
                new=AsyncMock(return_value=MagicMock(total_record_count=0)),
            ),
            patch.object(
                SqlApp,
                "fetch_schemas",
                new=AsyncMock(return_value=MagicMock(total_record_count=0)),
            ),
            patch.object(
                SqlApp,
                "fetch_tables",
                new=AsyncMock(return_value=MagicMock(total_record_count=0)),
            ),
            patch.object(
                SqlApp,
                "fetch_columns",
                new=AsyncMock(return_value=MagicMock(total_record_count=0)),
            ),
            patch.object(
                SqlApp, "transform_databases", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_schemas", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_tables", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "transform_columns", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(
                SqlApp, "upload_to_atlan", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
        ):
            import asyncio

            asyncio.run(app.run(input_))

        # build_output_path must NOT be called from run() — it would crash in workflow context
        mock_bop.assert_not_called()
