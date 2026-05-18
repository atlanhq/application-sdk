"""Unit tests for SqlApp consolidated SQL template (BLDX-968)."""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionTaskInput,
)
from application_sdk.templates.sql_app import SqlApp
from application_sdk.templates.sql_app_errors import (
    MapColumnUnimplementedError,
    MapDatabaseUnimplementedError,
    MapProcedureUnimplementedError,
    MapSchemaUnimplementedError,
    MapTableUnimplementedError,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class FakeSQLClient:
    """Mock SQL client that yields rows via ``run_query`` (the streaming API
    SqlApp's extract_* tasks consume).
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.loaded = False
        self._rows = rows or []
        self.last_query: str | None = None
        self.last_batch_size: int | None = None

    async def load(self, credentials=None):
        self.loaded = True

    async def close(self):
        pass

    async def run_query(self, query: str, batch_size: int = 100000):
        """Async generator — yields a single batch with all rows."""
        self.last_query = query
        self.last_batch_size = batch_size
        if self._rows:
            yield self._rows


def _mock_init_client(rows: list[dict[str, Any]]) -> AsyncMock:
    """Create an AsyncMock that returns a FakeSQLClient yielding *rows*."""
    return AsyncMock(return_value=FakeSQLClient(rows=rows))


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


def _make_task_input(output_path="/tmp/test", **kwargs):
    """Helper to create ExtractionTaskInput for testing."""
    defaults = {
        "workflow_id": "test-wf",
        "output_path": output_path,
        "output_prefix": "/tmp",
        "exclude_filter": "",
        "include_filter": "",
        "temp_table_regex": "",
    }
    defaults.update(kwargs)
    return ExtractionTaskInput(**defaults)


# ---------------------------------------------------------------------------
# build_task_input (BLDX-1138)
# ---------------------------------------------------------------------------


class TestBuildTaskInput:
    """BLDX-1138: build_task_input as public API."""

    def test_builds_extraction_task_input(self):
        src = ExtractionInput(
            workflow_id="wf-1",
            output_path="/out",
            output_prefix="/pfx",
            exclude_filter="^temp$",
            include_filter="^prod$",
            temp_table_regex="^tmp_",
        )
        result = SqlApp.build_task_input(ExtractionTaskInput, src)
        assert result.workflow_id == "wf-1"
        assert result.output_path == "/out"
        assert result.exclude_filter == "^temp$"
        assert result.include_filter == "^prod$"

    def test_builds_with_credential_ref(self):
        src = ExtractionInput(workflow_id="wf-2")
        cred_ref = CredentialRef(credential_guid="test-guid")
        result = SqlApp.build_task_input(ExtractionTaskInput, src, cred_ref=cred_ref)
        assert result.credential_ref.credential_guid == "test-guid"


# ---------------------------------------------------------------------------
# extract_* tasks — SQL stream → raw JSONL (no parquet)
# ---------------------------------------------------------------------------


class TestExtractTasks:
    """Each extract_* task streams SQL rows verbatim to raw/<entity>/records.json."""

    async def test_extract_databases_writes_raw_jsonl(self, app, tmp_path):
        rows = [{"database_name": "db1"}, {"database_name": "db2"}]
        input_ = _make_task_input(output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(rows)):
            result = await app.extract_databases(input_)

        assert result.total_record_count == 2
        assert result.typename == "database"

        raw_file = tmp_path / "raw" / "database" / "records.json"
        assert raw_file.exists()
        lines = raw_file.read_text().strip().split("\n")
        assert len(lines) == 2
        # Raw JSONL contains the verbatim SQL row dicts — no asset wrapping
        assert json.loads(lines[0]) == {"database_name": "db1"}

    async def test_extract_no_sql_returns_zero(self, app):
        app.fetch_database_sql = ""
        input_ = _make_task_input()
        result = await app.extract_databases(input_)
        assert result.total_record_count == 0
        assert result.typename == "database"

    async def test_extract_schemas_writes_raw_jsonl(self, app, tmp_path):
        rows = [{"schema_name": "public"}, {"schema_name": "private"}]
        input_ = _make_task_input(output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(rows)):
            result = await app.extract_schemas(input_)

        assert result.total_record_count == 2
        out = (tmp_path / "raw" / "schema" / "records.json").read_text()
        assert "public" in out
        # No mapper output at the extract stage
        assert "Schema" not in out

    async def test_extract_views_no_sql_returns_zero(self, app):
        input_ = _make_task_input()
        result = await app.extract_views(input_)
        assert result.total_record_count == 0

    async def test_extract_procedures_no_sql_returns_zero(self, app):
        input_ = _make_task_input()
        result = await app.extract_procedures(input_)
        assert result.total_record_count == 0

    async def test_extract_uses_batch_size_constant(self, app, tmp_path):
        """Verifies _EXTRACT_BATCH_SIZE is passed to client.run_query."""
        from application_sdk.templates.sql_app import _EXTRACT_BATCH_SIZE

        rows = [{"database_name": "db1"}]
        input_ = _make_task_input(output_path=str(tmp_path))

        client = FakeSQLClient(rows=rows)
        with patch.object(app, "_init_sql_client", AsyncMock(return_value=client)):
            await app.extract_databases(input_)

        assert client.last_batch_size == _EXTRACT_BATCH_SIZE


# ---------------------------------------------------------------------------
# transform_* tasks — raw JSONL → mapper → transformed JSONL
# ---------------------------------------------------------------------------


def _seed_raw(tmp_path, entity_type: str, records: list[dict]) -> None:
    """Helper: write raw/<entity>/records.json so transform_* has input."""
    raw_dir = tmp_path / "raw" / entity_type
    raw_dir.mkdir(parents=True)
    raw_file = raw_dir / "records.json"
    raw_file.write_text("\n".join(json.dumps(r) for r in records) + "\n")


class TestTransformTasks:
    """Each transform_* reads raw/<entity>/records.json and writes mapped JSONL."""

    async def test_transform_databases_uses_mapper(self, app, tmp_path):
        _seed_raw(tmp_path, "database", [{"database_name": "db1"}])
        input_ = _make_task_input(output_path=str(tmp_path))

        result = await app.transform_databases(input_)

        assert result.total_record_count == 1
        assert result.typename == "database"
        out = (tmp_path / "transformed" / "database" / "entities.json").read_text()
        entity = json.loads(out.strip())
        assert entity["typeName"] == "Database"
        assert entity["qualifiedName"].endswith("/db1")

    async def test_transform_tables_handles_multiple_rows(self, app, tmp_path):
        _seed_raw(
            tmp_path,
            "table",
            [{"table_name": n} for n in ("users", "orders", "products")],
        )
        input_ = _make_task_input(output_path=str(tmp_path))

        result = await app.transform_tables(input_)

        assert result.total_record_count == 3
        lines = (
            (tmp_path / "transformed" / "table" / "entities.json")
            .read_text()
            .strip()
            .split("\n")
        )
        assert len(lines) == 3
        for line in lines:
            assert json.loads(line)["typeName"] == "Table"

    async def test_transform_views_uses_map_table(self, app, tmp_path):
        """Views go through map_table — Atlan models View as a Table specialisation."""
        _seed_raw(tmp_path, "view", [{"table_name": "v1"}, {"table_name": "v2"}])
        input_ = _make_task_input(output_path=str(tmp_path))

        result = await app.transform_views(input_)

        assert result.total_record_count == 2
        out = (tmp_path / "transformed" / "view" / "entities.json").read_text()
        assert json.loads(out.split("\n")[0])["typeName"] == "Table"

    async def test_transform_no_raw_file_returns_zero(self, app, tmp_path):
        """When extract didn't run (no raw file), transform is a no-op."""
        input_ = _make_task_input(output_path=str(tmp_path))
        result = await app.transform_tables(input_)
        assert result.total_record_count == 0

    async def test_transform_procedures_no_raw_file(self, app, tmp_path):
        input_ = _make_task_input(output_path=str(tmp_path))
        result = await app.transform_procedures(input_)
        assert result.total_record_count == 0


# ---------------------------------------------------------------------------
# Asset mapper stubs
# ---------------------------------------------------------------------------


class TestAssetMapperStubs:
    """Asset mapper stubs raise NotImplementedError on base SqlApp."""

    def test_base_map_database_raises(self):
        base = SqlApp()
        with pytest.raises(MapDatabaseUnimplementedError):
            base.map_database({}, "conn/qn")

    def test_base_map_schema_raises(self):
        base = SqlApp()
        with pytest.raises(MapSchemaUnimplementedError):
            base.map_schema({}, "conn/qn")

    def test_base_map_table_raises(self):
        base = SqlApp()
        with pytest.raises(MapTableUnimplementedError):
            base.map_table({}, "conn/qn")

    def test_base_map_column_raises(self):
        base = SqlApp()
        with pytest.raises(MapColumnUnimplementedError):
            base.map_column({}, "conn/qn")

    def test_base_map_procedure_raises(self):
        base = SqlApp()
        with pytest.raises(MapProcedureUnimplementedError):
            base.map_procedure({}, "conn/qn")

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
            include_filter="^prod$",
            exclude_filter="^temp$",
        )
        result = app._prepare_sql(sql, input_)
        assert "^prod$" in result
        assert "^temp$" in result

    def test_default_include_is_wildcard(self, app):
        sql = "WHERE schema ~ '{normalized_include_regex}'"
        input_ = _make_task_input()
        result = app._prepare_sql(sql, input_)
        assert ".*" in result

    def test_default_exclude_is_nothing(self, app):
        sql = "WHERE schema !~ '{normalized_exclude_regex}'"
        input_ = _make_task_input()
        result = app._prepare_sql(sql, input_)
        assert "^$" in result

    def test_temp_table_regex_substitution(self, app):
        app.extract_temp_table_regex_table_sql = "AND t.name !~ '{exclude_table_regex}'"
        sql = "SELECT * FROM t {temp_table_regex_sql}"
        input_ = _make_task_input(temp_table_regex="^tmp_")
        result = app._prepare_sql(sql, input_)
        assert "AND t.name !~ '^tmp_'" in result

    def test_dict_filter_normalized_to_regex(self, app):
        sql = "WHERE schema ~ '{normalized_include_regex}'"
        input_ = _make_task_input(include_filter={"^prod$": ["^public$"]})
        result = app._prepare_sql(sql, input_)
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

    def _patch_extract_tasks(self):
        """Return list of patches that mock all extract_* + transform_* + upload_to_atlan."""
        return [
            patch.object(
                SqlApp,
                "extract_databases",
                new=AsyncMock(return_value=MagicMock(total_record_count=1)),
            ),
            patch.object(
                SqlApp,
                "extract_schemas",
                new=AsyncMock(return_value=MagicMock(total_record_count=1)),
            ),
            patch.object(
                SqlApp,
                "extract_tables",
                new=AsyncMock(return_value=MagicMock(total_record_count=2)),
            ),
            patch.object(
                SqlApp,
                "extract_columns",
                new=AsyncMock(return_value=MagicMock(total_record_count=10)),
            ),
            patch.object(
                SqlApp,
                "transform_databases",
                new=AsyncMock(return_value=MagicMock(total_record_count=1)),
            ),
            patch.object(
                SqlApp,
                "transform_schemas",
                new=AsyncMock(return_value=MagicMock(total_record_count=1)),
            ),
            patch.object(
                SqlApp,
                "transform_tables",
                new=AsyncMock(return_value=MagicMock(total_record_count=2)),
            ),
            patch.object(
                SqlApp,
                "transform_columns",
                new=AsyncMock(return_value=MagicMock(total_record_count=10)),
            ),
            patch.object(
                SqlApp, "upload_to_atlan", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
        ]

    async def test_uses_input_output_path_when_set(self):
        """When input.output_path is provided, use it directly (no workflow context needed)."""
        app = self._make_minimal_app()
        input_ = ExtractionInput(
            output_path="./local/tmp/artifacts/apps/test/workflows/wf-1/run-1"
        )

        patches = self._patch_extract_tasks()
        for p in patches:
            p.start()
        try:
            result = await app.run(input_)
        finally:
            for p in patches:
                p.stop()

        assert (
            "artifacts/apps/test/workflows/wf-1/run-1/transformed"
            in result.transformed_data_prefix
        )

    async def test_uses_workflow_info_when_output_path_empty(self):
        """When input.output_path is empty, derive path from workflow.info()."""
        app = self._make_minimal_app()

        mock_wf_info = MagicMock()
        mock_wf_info.workflow_id = "test-wf-123"
        mock_wf_info.run_id = "test-run-456"

        input_ = ExtractionInput(output_path="")  # empty — should use workflow context

        patches = [
            patch(
                "application_sdk.templates.sql_app._temporal_workflow.info",
                return_value=mock_wf_info,
            ),
            *self._patch_extract_tasks(),
        ]
        for p in patches:
            p.start()
        try:
            result = await app.run(input_)
        finally:
            for p in patches:
                p.stop()

        assert "test-wf-123" in result.transformed_data_prefix
        assert "test-run-456" in result.transformed_data_prefix
        assert result.transformed_data_prefix.endswith("/transformed")

    async def test_build_output_path_not_called_in_run(self):
        """build_output_path() (activity-only) must NOT be called from run()."""
        app = self._make_minimal_app()

        mock_wf_info = MagicMock()
        mock_wf_info.workflow_id = "wf-x"
        mock_wf_info.run_id = "run-x"

        input_ = ExtractionInput(output_path="")

        mock_bop_patch = patch("application_sdk.templates.sql_app.build_output_path")
        wf_info_patch = patch(
            "application_sdk.templates.sql_app._temporal_workflow.info",
            return_value=mock_wf_info,
        )

        mock_bop = mock_bop_patch.start()
        wf_info_patch.start()
        extract_patches = self._patch_extract_tasks()
        for p in extract_patches:
            p.start()
        try:
            await app.run(input_)
        finally:
            for p in extract_patches:
                p.stop()
            wf_info_patch.stop()
            mock_bop_patch.stop()

        # build_output_path must NOT be called from run() — it would crash in workflow context
        mock_bop.assert_not_called()
