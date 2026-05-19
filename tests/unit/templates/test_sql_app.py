"""Unit tests for SqlApp consolidated SQL template (BLDX-968)."""

from __future__ import annotations

import json
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.contracts.types import FileReference
from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionTaskInput,
    ExtractionTaskOutput,
    TransformInput,
    TransformOutput,
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
    """Helper to create a task input for testing.

    Returns a ``TransformInput`` because it's the broader of the two
    v3 contracts (extends ``ExtractionTaskInput`` with ``raw_file`` plus
    the legacy v2 ``typename`` / ``file_names`` / ``chunk_start`` fields).
    The ``extract_*`` activities only read ``ExtractionTaskInput`` fields
    so the extra ``TransformInput`` attributes are ignored on that side,
    and the ``transform_*`` activities can read ``input.raw_file``
    without hitting an ``AttributeError``. ``raw_file`` defaults to None.
    """
    defaults = {
        "workflow_id": "test-wf",
        "output_path": output_path,
        "output_prefix": "/tmp",
        "exclude_filter": "",
        "include_filter": "",
        "temp_table_regex": "",
    }
    defaults.update(kwargs)
    return TransformInput(**defaults)


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
# FileReference contract (BLDX-1281 / atlanhq/application-sdk#1787)
# ---------------------------------------------------------------------------
#
# Each extract_* must emit an ephemeral FileReference to raw/<entity>/records.json
# so the activity interceptor uploads it after the activity finishes and marks
# it durable. run() threads that durable ref into the matching transform_*
# input; the interceptor materialises it onto whichever worker pod runs the
# transform (SHA-256 sidecar verification handles the cross-worker case where
# extract and transform land on different pods). transform_* must also emit a
# transformed_file ref so downstream publish / upload tasks can consume the
# entities.json the same way.
#
# These tests pin that contract — both the "ref shape on output" and the
# "transform reads from input.raw_file.local_path" half of the handshake.


class TestExtractEmitsRawFileReference:
    """extract_* returns TransformOutput.raw_file pointing at the raw JSONL."""

    async def test_extract_databases_emits_raw_file(self, app, tmp_path):
        rows = [{"database_name": "db1"}]
        input_ = _make_task_input(output_path=str(tmp_path))

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(rows)):
            result = await app.extract_databases(input_)

        # FileReference points at the locally-written raw JSONL. It is
        # EPHEMERAL (is_durable=False, storage_path=None) — the activity
        # interceptor flips both fields after the activity returns.
        assert result.raw_file is not None
        assert result.raw_file.local_path == str(
            tmp_path / "raw" / "database" / "records.json"
        )
        assert result.raw_file.is_durable is False
        assert result.raw_file.storage_path is None

    async def test_extract_with_zero_rows_emits_no_raw_file(self, app, tmp_path):
        """When extract finds no rows, raw_file is None — not a ref to an
        empty file. The interceptor then has nothing to upload, and the
        downstream transform sees ``input.raw_file is None`` and returns
        count=0 cleanly (matches the historical 'extract returned 0 rows'
        contract that publish relies on)."""
        input_ = _make_task_input(output_path=str(tmp_path))
        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client([])):
            result = await app.extract_databases(input_)

        assert result.total_record_count == 0
        assert result.raw_file is None

    async def test_extract_no_sql_emits_no_raw_file(self, app):
        """No SQL configured ⇒ no extraction ⇒ no raw_file ref."""
        app.fetch_database_sql = ""
        input_ = _make_task_input()
        result = await app.extract_databases(input_)
        assert result.raw_file is None


class TestTransformInputRawDir:
    """``TransformInput.raw_dir`` is the recommended replacement for the
    legacy ``file_names`` / ``chunk_start`` / ``typename`` multi-file
    batch fields (BLDX-1281). The field is purely additive — existing
    consumers that read the legacy fields continue to work; new
    connectors that produce multi-file output should populate
    ``raw_dir`` with a directory-shaped ``FileReference`` so the
    activity interceptor handles cross-worker materialisation.
    """

    def test_raw_dir_defaults_to_none(self) -> None:
        """raw_dir is optional with a None default — pure addition."""
        input_ = TransformInput()
        assert input_.raw_dir is None

    def test_raw_dir_accepts_directory_filereference(self) -> None:
        """Pin the directory-FileReference shape new connectors should use."""
        ref = FileReference(
            local_path="/tmp/raw/columns/",
            storage_path="s3://bucket/raw/columns/",
            is_durable=True,
            file_count=42,
        )
        input_ = TransformInput(raw_dir=ref)
        assert input_.raw_dir is not None
        assert input_.raw_dir.local_path == "/tmp/raw/columns/"
        assert input_.raw_dir.is_durable is True
        assert input_.raw_dir.file_count == 42

    def test_legacy_fields_preserved_when_raw_dir_unset(self) -> None:
        """Backward-compat: existing v3 consumers reading file_names /
        chunk_start / typename keep working without populating raw_dir.
        """
        input_ = TransformInput(
            typename="column",
            file_names=["batch-0.parquet", "batch-1.parquet"],
            chunk_start=2,
        )
        assert input_.typename == "column"
        assert input_.file_names == ["batch-0.parquet", "batch-1.parquet"]
        assert input_.chunk_start == 2
        assert input_.raw_dir is None  # default — coexists with legacy fields

    def test_raw_dir_and_legacy_fields_can_coexist(self) -> None:
        """During the migration window, a connector may populate both
        ``raw_dir`` (new) and ``file_names`` (legacy compat) without
        conflict — useful when an extractor wants to test the new ref
        path while leaving the legacy reads intact.
        """
        ref = FileReference(local_path="/tmp/raw/")
        input_ = TransformInput(raw_dir=ref, file_names=["a.parquet"], typename="t")
        assert input_.raw_dir is not None
        assert input_.file_names == ["a.parquet"]
        assert input_.typename == "t"


class TestTransformConsumesRawFileReference:
    """transform_* reads from input.raw_file.local_path when populated."""

    async def test_transform_uses_raw_file_local_path(self, app, tmp_path):
        """When input.raw_file points at a custom local_path (e.g. a path
        the interceptor materialised under TEMPORARY_PATH on a different
        worker pod than the original extract), transform must read from
        there — NOT from the default ``output_path/raw/<entity>/records.json``
        location. This is the core of the cross-worker fix.
        """
        # Materialise the raw file at a location that does NOT match the
        # ``output_path/raw/database/records.json`` legacy default. If
        # transform fell back to the legacy path, it would find nothing
        # and return count=0; we want to assert it uses input.raw_file.
        materialised = tmp_path / "interceptor-staged" / "raw-from-s3.jsonl"
        materialised.parent.mkdir(parents=True, exist_ok=True)
        materialised.write_text(json.dumps({"database_name": "remote_db"}) + "\n")

        input_ = _make_task_input(
            output_path=str(tmp_path),
            raw_file=FileReference(
                local_path=str(materialised),
                storage_path="artifacts/.../raw/database/records.json",
                is_durable=True,
            ),
        )

        result = await app.transform_databases(input_)

        assert result.total_record_count == 1
        entities = (tmp_path / "transformed" / "database" / "entities.json").read_text()
        assert json.loads(entities)["qualifiedName"].endswith("/remote_db")

    async def test_transform_emits_transformed_file_reference(self, app, tmp_path):
        """transform_* must emit transformed_file pointing at entities.json
        as an ephemeral FileReference. The interceptor uploads it; the
        publish / upload step consumes the durable ref the same way
        transform consumed raw_file. Without this, the BLDX-1281 contract
        is half-complete and publish would still race on local-FS reads.
        """
        _seed_raw(tmp_path, "table", [{"table_name": "users"}])
        input_ = _make_task_input(output_path=str(tmp_path))

        result = await app.transform_tables(input_)

        assert result.transformed_file is not None
        assert result.transformed_file.local_path == str(
            tmp_path / "transformed" / "table" / "entities.json"
        )
        assert result.transformed_file.is_durable is False
        assert result.transformed_file.storage_path is None

    async def test_transform_no_input_ref_falls_back_to_legacy_path(
        self, app, tmp_path
    ):
        """If the caller doesn't thread a raw_file ref (older orchestrators,
        unit tests that seed the raw file directly), the transform falls
        back to ``<output_path>/raw/<entity>/records.json``. Preserves
        compatibility with subclasses that override ``run()``.
        """
        _seed_raw(tmp_path, "table", [{"table_name": "t1"}])
        input_ = _make_task_input(output_path=str(tmp_path))  # raw_file unset

        result = await app.transform_tables(input_)

        assert result.total_record_count == 1
        assert result.transformed_file is not None  # still emits the ref

    async def test_transform_zero_rows_emits_no_transformed_file(self, app, tmp_path):
        """When transform processes zero rows (e.g. raw file is empty or
        no raw_file ref was threaded), the transformed_file ref is None —
        no empty entities.json to upload, no spurious publish-time
        archival (the publish step interprets a missing transformed_file
        the same way it used to interpret a missing entities.json file).
        """
        input_ = _make_task_input(output_path=str(tmp_path))
        result = await app.transform_tables(input_)
        assert result.total_record_count == 0
        assert result.transformed_file is None


class TestRunThreadsRawFileRefs:
    """run() must thread each extract's raw_file ref into the matching transform."""

    async def test_run_passes_extract_raw_file_into_transform(self, app, tmp_path):
        """End-to-end: extract emits raw_file, run() passes it to transform,
        transform reads from it. Mocks both extract and transform to capture
        the inputs and assert the wiring.
        """
        # Build distinct refs per entity so we can assert the right ref
        # reaches the matching transform (no cross-wiring between e.g.
        # extract_schemas and transform_databases).
        refs = {
            "database": FileReference(
                local_path=str(tmp_path / "raw" / "database" / "records.json"),
                storage_path="s3://.../raw/database/records.json",
                is_durable=True,
            ),
            "schema": FileReference(
                local_path=str(tmp_path / "raw" / "schema" / "records.json"),
                storage_path="s3://.../raw/schema/records.json",
                is_durable=True,
            ),
            "table": FileReference(
                local_path=str(tmp_path / "raw" / "table" / "records.json"),
                storage_path="s3://.../raw/table/records.json",
                is_durable=True,
            ),
            "column": FileReference(
                local_path=str(tmp_path / "raw" / "column" / "records.json"),
                storage_path="s3://.../raw/column/records.json",
                is_durable=True,
            ),
        }

        captured: dict[str, ExtractionTaskInput] = {}

        def make_extract(entity: str):
            async def _extract(_input):
                return ExtractionTaskOutput(
                    typename=entity,
                    total_record_count=1,
                    raw_file=refs[entity],
                )

            return _extract

        def make_transform(entity: str):
            async def _transform(input_):
                captured[entity] = input_
                return TransformOutput(typename=entity, total_record_count=1)

            return _transform

        with (
            patch.object(
                app, "extract_databases", side_effect=make_extract("database")
            ),
            patch.object(app, "extract_schemas", side_effect=make_extract("schema")),
            patch.object(app, "extract_tables", side_effect=make_extract("table")),
            patch.object(app, "extract_columns", side_effect=make_extract("column")),
            patch.object(
                app, "transform_databases", side_effect=make_transform("database")
            ),
            patch.object(
                app, "transform_schemas", side_effect=make_transform("schema")
            ),
            patch.object(app, "transform_tables", side_effect=make_transform("table")),
            patch.object(
                app, "transform_columns", side_effect=make_transform("column")
            ),
            patch.object(
                app, "upload_to_atlan", new=AsyncMock(return_value=MagicMock())
            ),
            patch.object(app, "_resolve_credential_ref", return_value=None),
            patch(
                "application_sdk.templates.sql_app._temporal_workflow.info",
                return_value=MagicMock(workflow_id="wf-test", run_id="run-test"),
            ),
        ):
            await app.run(
                ExtractionInput(
                    workflow_id="wf-test",
                    output_path=str(tmp_path),
                    credential_guid="test-guid",
                    extraction_method="direct",
                )
            )

        # Each transform_* must have been invoked with its OWN extract's
        # raw_file ref, not someone else's.
        for entity, expected_ref in refs.items():
            assert entity in captured, f"transform_{entity} not invoked"
            actual_ref = captured[entity].raw_file
            assert actual_ref is not None, f"transform_{entity} got no ref"
            assert actual_ref.storage_path == expected_ref.storage_path, (
                f"cross-wired ref: transform_{entity} got ref for "
                f"{actual_ref.storage_path} instead of {expected_ref.storage_path}"
            )


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
        """Return list of patches that mock all extract_* + transform_* + upload_to_atlan.

        Use real ``ExtractionTaskOutput`` instances (not MagicMocks) for
        the extract returns — ``run()`` reads ``.raw_file`` and threads
        it into ``_build_transform_input`` which Pydantic-validates the
        ref against ``FileReference``; MagicMock auto-attrs would fail
        that validation (BLDX-1281).
        """
        return [
            patch.object(
                SqlApp,
                "extract_databases",
                new=AsyncMock(
                    return_value=ExtractionTaskOutput(
                        typename="database", total_record_count=1, raw_file=None
                    )
                ),
            ),
            patch.object(
                SqlApp,
                "extract_schemas",
                new=AsyncMock(
                    return_value=ExtractionTaskOutput(
                        typename="schema", total_record_count=1, raw_file=None
                    )
                ),
            ),
            patch.object(
                SqlApp,
                "extract_tables",
                new=AsyncMock(
                    return_value=ExtractionTaskOutput(
                        typename="table", total_record_count=2, raw_file=None
                    )
                ),
            ),
            patch.object(
                SqlApp,
                "extract_columns",
                new=AsyncMock(
                    return_value=ExtractionTaskOutput(
                        typename="column", total_record_count=10, raw_file=None
                    )
                ),
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
