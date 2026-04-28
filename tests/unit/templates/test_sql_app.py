"""Unit tests for SqlApp consolidated SQL template (BLDX-968)."""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    FetchColumnsInput,
    FetchDatabasesInput,
    FetchSchemasInput,
    FetchTablesInput,
    FetchViewsInput,
    FetchProceduresInput,
    TransformInput,
)
from application_sdk.templates.sql_app import SqlApp


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class FakeSQLClient:
    """Mock SQL client for testing."""

    def __init__(self):
        self.loaded = False
        self._results = pd.DataFrame()

    async def load(self, credentials=None):
        self.loaded = True

    async def close(self):
        pass

    async def get_results(self, sql: str) -> pd.DataFrame:
        return self._results


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
        mock_ref = MagicMock()
        result = SqlApp.build_task_input(FetchSchemasInput, src, cred_ref=mock_ref)
        assert result.credential_ref is mock_ref

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
        FakeSQLClient._results = pd.DataFrame({"database_name": ["db1", "db2"]})
        input_ = _make_task_input(FetchDatabasesInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", return_value=FakeSQLClient()):
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
        FakeSQLClient._results = pd.DataFrame({"schema_name": ["public", "private"]})
        input_ = _make_task_input(FetchSchemasInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", return_value=FakeSQLClient()):
            result = await app.fetch_schemas(input_)

        assert result.total_record_count == 2
        parquet_dir = tmp_path / "raw" / "schema"
        assert parquet_dir.exists()
        assert len(list(parquet_dir.glob("*.parquet"))) == 1

    async def test_fetch_tables_returns_count(self, app, tmp_path):
        FakeSQLClient._results = pd.DataFrame(
            {"table_name": ["users", "orders", "products"]}
        )
        input_ = _make_task_input(FetchTablesInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", return_value=FakeSQLClient()):
            result = await app.fetch_tables(input_)

        assert result.total_record_count == 3

    async def test_fetch_columns_returns_count(self, app, tmp_path):
        FakeSQLClient._results = pd.DataFrame(
            {"column_name": ["id", "name", "email", "created_at"]}
        )
        input_ = _make_task_input(FetchColumnsInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", return_value=FakeSQLClient()):
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
        FakeSQLClient._results = pd.DataFrame({"view_name": ["v1", "v2"]})
        input_ = _make_task_input(FetchViewsInput, output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", return_value=FakeSQLClient()):
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
        output_file = tmp_path / "transformed" / "table" / "entities.jsonl"
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
