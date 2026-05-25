"""Unit tests for SqlApp consolidated SQL template (BLDX-968)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionTaskInput,
    ExtractionTaskOutput,
    PrimeAuthOutput,
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

    async def get_results(self, query: str):
        """Single-query result. Used by ``prime_sql_auth`` (BLDX-1295)
        to issue a ``SELECT 1`` probe — we don't care about the value,
        only that the connection + auth completed."""
        self.last_query = query
        return self._rows


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

        # FileReference points at the locally-written raw JSONL.
        # is_durable=False (the activity interceptor flips it after
        # the activity returns), but storage_path is pre-set to the
        # canonical run-scoped key — see the comment in
        # ``_extract_entity``'s emission site for why we pin the
        # storage key here instead of letting the interceptor
        # auto-generate a ``file_refs/<uuid>`` path. The persist
        # mechanism honours the pre-set key.
        assert result.raw_file is not None
        assert result.raw_file.local_path == str(
            tmp_path / "raw" / "database" / "records.json"
        )
        assert result.raw_file.is_durable is False
        # storage_path == get_object_store_prefix(local_path), which
        # in tests with output_path=tmp_path resolves to the tmp_path
        # itself (no TEMPORARY_PATH prefix to strip). In production
        # the local_path is under TEMPORARY_PATH, so the strip
        # yields ``<run_prefix>/raw/<entity>/records.json``.
        assert result.raw_file.storage_path is not None
        assert result.raw_file.storage_path.endswith(
            "/raw/database/records.json"
        ), f"unexpected storage_path: {result.raw_file.storage_path!r}"
        # Tier stays TRANSIENT (the semantically correct choice — the
        # raw file is an intermediate extract→transform handoff and
        # gets auto-cleaned at run end by ``cleanup_storage``).
        assert result.raw_file.tier == StorageTier.TRANSIENT

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


class TestCanonicalStoragePaths:
    """Regression guard: ``_extract_entity`` / ``_transform_entity``
    emit ``FileReference`` objects whose ``storage_path`` resolves to
    the canonical entity-typed key
    (``<run_prefix>/raw/<entity>/records.json`` /
    ``<run_prefix>/transformed/<entity>/entities.json``) — **not** the
    UUID-named ``<run_prefix>/file_refs/<uuid>.json`` fallback the bare
    ``FileReference.from_local()`` constructor used to produce.

    Why this matters: the downstream publish step discovers transformed
    assets by walking ``transformed/<entity>/`` prefixes. Anything that
    only lands under ``file_refs/<uuid>.json`` is invisible to publish
    and the entity gets archived as "removed from source" on the next
    run.

    Production incident (customer tenant, mysql-app):
        * ``extract_databases`` on pod A → wrote
          ``raw/database/records.json`` to pod A local FS.
        * ``transform_databases`` on pod B (different replica) →
          consumed raw via the interceptor's materialise handshake,
          wrote ``transformed/database/entities.json`` to pod B local
          FS, emitted a ``transformed_file`` FileReference.
        * ``upload_to_atlan`` on pod C → its directory walk found
          pod C's local FS empty (none of the per-entity transform
          files were ever on pod C) → uploaded nothing under
          ``transformed/database/``.
        * The interceptor DID upload pod B's ``entities.json`` to
          object store — but to a UUID-named ``file_refs/<uuid>.json``
          key that publish doesn't discover.
        * Publish read ``transformed/database/`` → found nothing →
          archived the database asset on the customer tenant.

    The fix pins ``storage_path`` on the ref at the SqlApp emission
    sites so the interceptor's persist lands the file at the canonical
    path directly — no dependency on ``upload_to_atlan``'s
    cross-pod-fragile directory walk.
    """

    async def test_extract_raw_file_persists_at_canonical_key(self, app, tmp_path):
        """Extract emits a ref with ``storage_path`` pre-set to the
        canonical key. Persist honours it and uploads to that exact
        location — NOT under ``file_refs/<uuid>``.
        """
        from application_sdk.storage.factory import create_local_store
        from application_sdk.storage.ops import exists
        from application_sdk.storage.reference import persist_file_reference

        rows = [{"database_name": "db1"}]
        input_ = _make_task_input(output_path=str(tmp_path))
        store = create_local_store(tmp_path / "store")

        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(rows)):
            result = await app.extract_databases(input_)

        assert result.raw_file is not None
        assert result.raw_file.storage_path is not None
        # Pre-persist: storage_path is the canonical key shape.
        assert result.raw_file.storage_path.endswith("/raw/database/records.json")

        # Persist must honour the pre-set storage_path (the key
        # contract change in storage/reference.py).
        durable = await persist_file_reference(
            store, result.raw_file, output_path="ignored-because-pre-set"
        )
        assert durable.storage_path == result.raw_file.storage_path, (
            f"persist must honour pre-set storage_path; got "
            f"{durable.storage_path!r} expected {result.raw_file.storage_path!r}"
        )
        # File actually landed at the canonical key.
        assert await exists(durable.storage_path, store, normalize=False)

    async def test_transform_transformed_file_persists_at_canonical_key(
        self, app, tmp_path
    ):
        """Same guard for the transform → publish handoff — the key
        path that broke on the customer tenant."""
        from application_sdk.storage.factory import create_local_store
        from application_sdk.storage.ops import exists
        from application_sdk.storage.reference import persist_file_reference

        _seed_raw(tmp_path, "database", [{"database_name": "remote_db"}])
        input_ = _make_task_input(output_path=str(tmp_path))
        store = create_local_store(tmp_path / "store")

        result = await app.transform_databases(input_)

        assert result.transformed_file is not None
        assert result.transformed_file.storage_path is not None
        assert result.transformed_file.storage_path.endswith(
            "/transformed/database/entities.json"
        )

        durable = await persist_file_reference(
            store, result.transformed_file, output_path="ignored-because-pre-set"
        )
        assert durable.storage_path == result.transformed_file.storage_path
        assert await exists(durable.storage_path, store, normalize=False)

    async def test_no_file_refs_uuid_orphan_when_storage_path_pre_set(
        self, app, tmp_path
    ):
        """Pin the failure mode: when storage_path is pre-set, the
        upload must land ONLY at the canonical key — NOT also at
        ``file_refs/<uuid>``. The customer-bucket repro had
        ``file_refs/<uuid>.json`` orphans next to (partial)
        ``transformed/<entity>/entities.json`` files — publish ignored
        the orphans and archived assets whose canonical key was
        missing. This test confirms no orphan is created.
        """
        from application_sdk.storage.batch import list_keys
        from application_sdk.storage.factory import create_local_store
        from application_sdk.storage.reference import persist_file_reference

        _seed_raw(tmp_path, "database", [{"database_name": "x"}])
        input_ = _make_task_input(output_path=str(tmp_path))
        store = create_local_store(tmp_path / "store")

        result = await app.transform_databases(input_)
        assert result.transformed_file is not None

        await persist_file_reference(
            store, result.transformed_file, output_path="run-prefix"
        )

        # Walk the store — there must be NO file_refs/ key for this
        # ref. The only keys we accept are at the canonical
        # transformed/database/... prefix.
        all_keys = await list_keys("", store, normalize=False)
        file_refs_keys = [k for k in all_keys if "file_refs/" in k]
        assert not file_refs_keys, (
            "persist created file_refs/<uuid> orphan(s) — publish cannot "
            "discover these and the entity will be archived: "
            f"{file_refs_keys}"
        )

    async def test_full_pipeline_writes_only_canonical_keys_no_file_refs_orphans(
        self, app, tmp_path
    ):
        """End-to-end multi-entity guard for the customer-tenant
        production incident.

        Reproduces the customer scenario:
            * 4 entities (database, schema, table, column) extracted
              and transformed in parallel.
            * Every emitted ``FileReference`` is persisted (the
              interceptor pattern).
            * Walks the store and asserts the final layout matches
              what publish expects:

                <run_prefix>/raw/database/records.json
                <run_prefix>/raw/schema/records.json
                <run_prefix>/raw/table/records.json
                <run_prefix>/raw/column/records.json
                <run_prefix>/transformed/database/entities.json
                <run_prefix>/transformed/schema/entities.json
                <run_prefix>/transformed/table/entities.json
                <run_prefix>/transformed/column/entities.json

              and NOTHING under ``file_refs/<uuid>.json``.

        Customer-bucket S3 listing for the failing run had a mix of
        canonical ``transformed/<entity>/entities.json`` (4 entities,
        missing the database one) AND ``file_refs/<uuid>.json``
        orphans (7 entries — content of the missing transformed
        files, but at UUID keys publish couldn't discover). Publish
        archived the missing-canonical entity as 'removed from
        source'. This test catches that pattern before it ships.
        """
        from application_sdk.storage.batch import list_keys
        from application_sdk.storage.factory import create_local_store
        from application_sdk.storage.reference import persist_file_reference

        store = create_local_store(tmp_path / "store")
        input_ = _make_task_input(output_path=str(tmp_path))

        # ── extract all 4 standard entities (the SqlApp.run() parallel set) ──
        entity_sql_rows = {
            "database": [{"database_name": "prod"}],
            "schema": [{"schema_name": "public"}, {"schema_name": "private"}],
            "table": [
                {"table_name": "users"},
                {"table_name": "orders"},
                {"table_name": "products"},
            ],
            "column": [{"column_name": "id"}, {"column_name": "name"}],
        }
        extract_methods = {
            "database": app.extract_databases,
            "schema": app.extract_schemas,
            "table": app.extract_tables,
            "column": app.extract_columns,
        }
        extract_results = {}
        for entity, rows in entity_sql_rows.items():
            with patch.object(
                app, "_init_sql_client", side_effect=_mock_init_client(rows)
            ):
                extract_results[entity] = await extract_methods[entity](input_)

        # ── persist every raw_file (interceptor pattern, simulated) ──
        for entity, result in extract_results.items():
            assert result.raw_file is not None, f"extract_{entity}s returned no ref"
            durable = await persist_file_reference(
                store, result.raw_file, output_path="ignored"
            )
            assert durable.storage_path == result.raw_file.storage_path
            assert durable.storage_path.endswith(
                f"/raw/{entity}/records.json"
            ), f"raw_file for {entity} not at canonical key: {durable.storage_path!r}"

        # ── transform all 4 entities (each with its raw_file threaded in) ──
        transform_methods = {
            "database": app.transform_databases,
            "schema": app.transform_schemas,
            "table": app.transform_tables,
            "column": app.transform_columns,
        }
        for entity in entity_sql_rows:
            transform_input = _make_task_input(
                output_path=str(tmp_path),
                raw_file=extract_results[entity].raw_file,
            )
            transform_result = await transform_methods[entity](transform_input)
            assert (
                transform_result.transformed_file is not None
            ), f"transform_{entity}s returned no ref"
            # Persist the transformed ref.
            durable = await persist_file_reference(
                store, transform_result.transformed_file, output_path="ignored"
            )
            assert durable.storage_path.endswith(
                f"/transformed/{entity}/entities.json"
            ), (
                f"transformed_file for {entity} not at canonical key: "
                f"{durable.storage_path!r}"
            )

        # ── Walk the store: verify the layout matches publish expectations ──
        all_keys = sorted(await list_keys("", store, normalize=False))
        # Strip sidecars (we only care about the data files for this check).
        data_keys = [k for k in all_keys if not k.endswith(".sha256")]

        # Expectation: 4 canonical raw + 4 canonical transformed = 8 data files.
        expected_suffixes = {f"/raw/{e}/records.json" for e in entity_sql_rows} | {
            f"/transformed/{e}/entities.json" for e in entity_sql_rows
        }
        actual_suffixes = {
            "/" + "/".join(k.rsplit("/", 3)[-3:])
            for k in data_keys
            if any(k.endswith(s) for s in expected_suffixes)
        }
        assert actual_suffixes == expected_suffixes, (
            f"missing canonical keys for some entities. "
            f"expected: {expected_suffixes}, got: {actual_suffixes}, "
            f"all data keys: {data_keys}"
        )

        # ── And ZERO file_refs/<uuid> orphans. The customer-bucket
        # repro had 7 such orphans — publish ignored them and the
        # entity whose canonical key was missing got archived.
        file_refs_orphans = [k for k in data_keys if "file_refs/" in k]
        assert not file_refs_orphans, (
            "persist created file_refs/<uuid> orphan(s) — the customer "
            "incident pattern. Publish discovers transformed assets by "
            "walking transformed/<entity>/ and CANNOT find these UUID "
            f"keys: {file_refs_orphans}"
        )

    async def test_cross_pod_handshake_preserved_with_canonical_keys(
        self, app, tmp_path
    ):
        """Pin the BLDX-1281 cross-pod fault tolerance — with the
        canonical-key change, the materialise step on a different pod
        must still find the raw file and reproduce its contents.

        Scenario: pod A extracts (writes raw, persists ref), pod A's
        local file vanishes (simulating pod B picking up the
        transform), materialise re-downloads from object store, then
        transform reads the materialised file and produces a
        transformed ref at the canonical key.
        """
        from pathlib import Path

        from application_sdk.storage.factory import create_local_store
        from application_sdk.storage.reference import (
            materialize_file_reference,
            persist_file_reference,
        )

        rows = [{"database_name": "db1"}, {"database_name": "db2"}]
        input_ = _make_task_input(output_path=str(tmp_path))
        store = create_local_store(tmp_path / "store")

        # Pod A: extract → persist → file in object store at canonical key.
        with patch.object(app, "_init_sql_client", side_effect=_mock_init_client(rows)):
            extract_result = await app.extract_databases(input_)
        assert extract_result.raw_file is not None
        durable_raw = await persist_file_reference(
            store, extract_result.raw_file, output_path="ignored"
        )
        # Confirm canonical key, not file_refs/<uuid>.
        assert durable_raw.storage_path is not None
        assert durable_raw.storage_path.endswith("/raw/database/records.json")

        # Pod B simulation: delete pod A's local file.
        Path(extract_result.raw_file.local_path).unlink()

        # Materialise on pod B — re-download from object store at the
        # canonical key. This is the BLDX-1281 cross-pod recovery
        # path; the canonical-key change does not break it because
        # materialise just reads ``ref.storage_path`` (wherever the
        # caller pinned it).
        materialised = await materialize_file_reference(store, durable_raw)
        assert materialised.local_path is not None
        assert Path(materialised.local_path).exists(), (
            "materialise must rebuild the local file from the canonical "
            "storage_path even when the original pod's copy is gone — "
            "the BLDX-1281 cross-pod guarantee, preserved by the "
            "canonical-key change."
        )

        # Pod B transform now has access to raw via the materialised
        # ref. Verify the transformed ref also lands at canonical.
        transform_input = _make_task_input(
            output_path=str(tmp_path), raw_file=materialised
        )
        transform_result = await app.transform_databases(transform_input)
        assert transform_result.transformed_file is not None
        assert transform_result.transformed_file.storage_path is not None
        assert transform_result.transformed_file.storage_path.endswith(
            "/transformed/database/entities.json"
        )

    async def test_cross_pod_multi_entity_no_upload_to_atlan_dependency(
        self, app, tmp_path
    ):
        """Pin the **specific cross-pod failure mode** that caused the
        customer incident: extract and transform activities for
        different entities scheduled on different pods, with NO
        ``upload_to_atlan`` running (or it ran on a pod with empty
        local FS — equivalent).

        Pre-fix flow on the customer tenant:
            * extract_databases on pod A → wrote
              ``raw/database/records.json`` locally + emitted ref.
            * transform_databases on pod B → consumed via materialise,
              wrote ``transformed/database/entities.json`` locally +
              emitted ref. Interceptor uploaded both refs to
              ``file_refs/<uuid>.json``.
            * extract_schemas / transform_schemas on pod C →
              ``file_refs/<other-uuid>.json`` for schema's refs.
            * upload_to_atlan on pod D → local FS empty → uploaded
              nothing canonical → publish saw only partial
              ``transformed/<entity>/`` → archived missing entities.

        Post-fix: pre-set ``storage_path`` on every ref means persist
        lands the file at the canonical key from whichever pod ran
        the activity — no dependency on ``upload_to_atlan``'s local
        FS state. This test simulates that distributed schedule.
        """
        from pathlib import Path

        from application_sdk.storage.batch import list_keys
        from application_sdk.storage.factory import create_local_store
        from application_sdk.storage.reference import (
            materialize_file_reference,
            persist_file_reference,
        )

        store = create_local_store(tmp_path / "store")

        # Each entity runs on its own (different) pod — separate tmp
        # subdirs simulate per-pod local FS isolation.
        pods = {
            entity: tmp_path / f"pod-{entity}"
            for entity in ("database", "schema", "table", "column")
        }
        for pod_dir in pods.values():
            pod_dir.mkdir()

        entity_rows = {
            "database": [{"database_name": "db1"}],
            "schema": [{"schema_name": "public"}],
            "table": [{"table_name": "users"}],
            "column": [{"column_name": "id"}],
        }
        extract_methods = {
            "database": app.extract_databases,
            "schema": app.extract_schemas,
            "table": app.extract_tables,
            "column": app.extract_columns,
        }
        transform_methods = {
            "database": app.transform_databases,
            "schema": app.transform_schemas,
            "table": app.transform_tables,
            "column": app.transform_columns,
        }

        # ── Each entity: extract on its pod → persist → DELETE local
        # file (simulating that pod going away) → transform on a
        # different pod that materialises from object store. ──
        for entity, rows in entity_rows.items():
            extract_pod = pods[entity]
            extract_input = _make_task_input(output_path=str(extract_pod))

            with patch.object(
                app, "_init_sql_client", side_effect=_mock_init_client(rows)
            ):
                extract_result = await extract_methods[entity](extract_input)

            assert extract_result.raw_file is not None
            durable_raw = await persist_file_reference(
                store, extract_result.raw_file, output_path="ignored"
            )
            assert durable_raw.storage_path.endswith(f"/raw/{entity}/records.json")

            # Simulate the extract pod going away — its local FS is
            # gone before the transform runs.
            Path(extract_result.raw_file.local_path).unlink()

            # Transform runs on a DIFFERENT pod (one of the other
            # entities' pod dirs — to be explicit, none of these
            # pods have the raw file locally). Materialise via the
            # interceptor pattern.
            transform_pod_entities = [e for e in pods if e != entity]
            transform_pod = pods[transform_pod_entities[0]]
            materialised = await materialize_file_reference(store, durable_raw)
            assert Path(materialised.local_path).exists(), (
                f"materialise must rebuild raw file for {entity} on a "
                f"different pod — BLDX-1281 cross-pod guarantee"
            )

            transform_input = _make_task_input(
                output_path=str(transform_pod), raw_file=materialised
            )
            transform_result = await transform_methods[entity](transform_input)

            assert transform_result.transformed_file is not None
            durable_transformed = await persist_file_reference(
                store, transform_result.transformed_file, output_path="ignored"
            )
            assert durable_transformed.storage_path.endswith(
                f"/transformed/{entity}/entities.json"
            )

        # ── Verify the final store layout: every entity has its
        # canonical raw + transformed keys, NO file_refs/<uuid>
        # orphans, regardless of which "pod" each activity ran on. ──
        all_keys = await list_keys("", store, normalize=False)
        data_keys = [k for k in all_keys if not k.endswith(".sha256")]

        for entity in entity_rows:
            assert any(
                k.endswith(f"/raw/{entity}/records.json") for k in data_keys
            ), f"missing canonical raw key for {entity}: {data_keys}"
            assert any(
                k.endswith(f"/transformed/{entity}/entities.json") for k in data_keys
            ), f"missing canonical transformed key for {entity}: {data_keys}"

        # The smoking gun: NO file_refs/<uuid> orphans. The customer
        # incident had these next to partial transformed/ paths.
        orphans = [k for k in data_keys if "file_refs/" in k]
        assert not orphans, (
            "cross-pod multi-entity persist created file_refs/<uuid> "
            f"orphan(s) — publish will archive missing entities: {orphans}"
        )

    async def test_persist_honours_pre_set_storage_path(self, tmp_path):
        """Direct unit test on the contract change in
        ``persist_file_reference``: when ``ref.storage_path`` is set
        pre-persist, persist uses it as the upload key (instead of
        auto-generating a ``file_refs/<uuid>`` path). Guards a future
        refactor that might silently revert this.
        """
        from application_sdk.storage.factory import create_local_store
        from application_sdk.storage.reference import persist_file_reference

        f = tmp_path / "x.json"
        f.write_text("{}")
        store = create_local_store(tmp_path / "store")

        ref = FileReference(
            local_path=str(f),
            storage_path="my/canonical/key.json",
            tier=StorageTier.TRANSIENT,
        )
        durable = await persist_file_reference(store, ref, output_path="ignored")
        assert durable.storage_path == "my/canonical/key.json"

    async def test_persist_falls_back_to_uuid_when_no_storage_path(self, tmp_path):
        """No regression on the legacy contract: refs WITHOUT a
        pre-set storage_path still get UUID-named keys. Guards
        existing callers (``storage/formats/parquet.py``,
        ``storage/rolling.py``) that don't pin a storage_path.
        """
        from application_sdk.storage.factory import create_local_store
        from application_sdk.storage.reference import persist_file_reference

        f = tmp_path / "x.json"
        f.write_text("{}")
        store = create_local_store(tmp_path / "store")

        ref = FileReference.from_local(f)  # default TRANSIENT, no storage_path
        assert ref.storage_path is None

        durable = await persist_file_reference(
            store, ref, output_path="artifacts/apps/test/workflows/wf/run"
        )
        assert durable.storage_path is not None
        assert "/file_refs/" in durable.storage_path

    def test_from_local_accepts_tier_kwarg(self, tmp_path):
        """``FileReference.from_local`` accepts an explicit ``tier`` kwarg.

        Default stays ``TRANSIENT`` for the common task-to-task case;
        callers can opt into ``RETAINED`` (handoffs that cross
        deployment boundaries, e.g. SDR → in-tenant publish) or
        ``PERSISTENT`` (files that must survive across multiple runs).
        Pass-through verified here.
        """
        f = tmp_path / "x.json"
        f.write_text("{}")

        default_ref = FileReference.from_local(f)
        retained_ref = FileReference.from_local(f, tier=StorageTier.RETAINED)
        persistent_ref = FileReference.from_local(f, tier=StorageTier.PERSISTENT)

        assert default_ref.tier == StorageTier.TRANSIENT
        assert retained_ref.tier == StorageTier.RETAINED
        assert persistent_ref.tier == StorageTier.PERSISTENT


class TestGetObjectStorePrefixCrossPlatform:
    """Pin ``get_object_store_prefix`` output shape so SqlApp's
    canonical ``storage_path`` is always a forward-slash, drive-less,
    leading-slash-stripped relative key — across every supported
    platform.

    Why a dedicated test: the function is the bridge between
    OS-specific local paths and object-store keys. Windows runners
    surfaced two bugs in succession:

    1. Backslash separators leaking into ``storage_path`` (fixed
       in a prior commit on this branch).
    2. Drive letter (``C:\\``) leaking into ``storage_path`` so
       obstore's LocalFileSystem backend tried to walk
       ``<store_root>\\C:\\...`` and rejected the absolute-path
       composition.

    The function must return a relative-style key regardless of
    whether the caller passed a POSIX-absolute, Windows-absolute,
    or already-relative path. These tests pin that invariant so
    a future refactor can't silently restore the Windows-only
    breakage (Linux/macOS CI passes either way because POSIX local
    stores tolerate the extra leading slash).
    """

    def test_no_backslashes_in_output(self):
        """Forward-slash convention — never backslash, regardless of platform."""
        from application_sdk.execution import get_object_store_prefix

        # Simulate Windows-style separators on any platform by passing
        # a literal backslash string. The function uses ``os.path.sep``
        # for normalization, so on POSIX this becomes a no-op (the
        # input has no POSIX separators); on Windows the backslashes
        # would have been normalized in the OS-aware branch but the
        # test would still pass.
        out = get_object_store_prefix("some/relative/key.json")
        assert "\\" not in out, f"backslash leaked into key: {out!r}"

    def test_strips_leading_slash(self):
        """Leading slash isn't valid in object-store keys — strip it."""
        from application_sdk.execution import get_object_store_prefix

        out = get_object_store_prefix("/already/relative/key.json")
        # The function takes either local paths or object-store paths;
        # for both, leading separators should be stripped.
        assert not out.startswith("/"), f"leading slash leaked: {out!r}"

    def test_relative_input_passes_through_with_separator_normalization(self):
        """A relative path the caller passed as-is round-trips with
        only separator + leading-slash normalization."""
        from application_sdk.execution import get_object_store_prefix

        out = get_object_store_prefix("artifacts/apps/foo/file.json")
        assert out == "artifacts/apps/foo/file.json"

    def test_windows_drive_letter_stripped(self):
        """Windows-absolute input like ``C:\\Users\\foo\\bar`` must
        produce a drive-less, forward-slash relative key.

        Direct unit test on the helper — uses ``os.path.splitdrive``
        which is a no-op on POSIX, so this test exercises real
        behaviour only on Windows. On POSIX it's a smoke check that
        the function doesn't accidentally split a colon-containing
        POSIX path (POSIX has no drive notion).
        """
        import os

        from application_sdk.execution import get_object_store_prefix

        # Build a path with the platform's own separator + a drive-
        # like prefix. On POSIX ``os.path.splitdrive`` returns
        # ``("", path)`` so the drive-strip is a no-op and we get
        # back the same (separator-normalized, leading-slash-stripped)
        # path. On Windows the drive letter IS stripped.
        if os.path.sep == "\\":
            # Windows: real drive letter handling.
            out = get_object_store_prefix("C:\\Users\\runner\\raw\\db\\records.json")
            assert "C:" not in out, f"drive letter leaked: {out!r}"
            assert "\\" not in out, f"backslash leaked: {out!r}"
            assert out == "Users/runner/raw/db/records.json"
        else:
            # POSIX: ``os.path.splitdrive`` is a no-op. Just verify
            # the function returns a normalized relative form for
            # an absolute POSIX path under tmp.
            out = get_object_store_prefix("/tmp/runner/raw/db/records.json")
            assert not out.startswith("/")
            assert "\\" not in out

    def test_strips_trailing_slash(self):
        """Trailing slash must not leak into object-store keys."""
        from application_sdk.execution import get_object_store_prefix

        out = get_object_store_prefix("datasets/sales/2024/")
        assert not out.endswith("/"), f"trailing slash leaked: {out!r}"
        assert out == "datasets/sales/2024"


class TestTransformInputLegacyFields:
    """Sanity tests for the deprecated-but-retained fields on
    :class:`TransformInput` (``file_names`` / ``chunk_start`` /
    ``typename``). They remain on the schema so existing v3 consumers
    keep deserialising without ``AttributeError``; the SDK itself
    never populates them.
    """

    def test_legacy_fields_default_to_no_op_values(self) -> None:
        """The schema defaults make every legacy read a no-op:
        ``file_names`` is an empty list (``if input.file_names:`` is
        falsy) and ``chunk_start`` is ``0``. Pin this so a future
        change to the defaults can't silently flip dead branches in
        downstream consumers into live ones.
        """
        input_ = TransformInput()
        assert input_.file_names == []
        assert input_.chunk_start == 0
        assert input_.typename == ""

    def test_legacy_fields_accept_caller_supplied_values(self) -> None:
        """v3 consumers that dispatch via ``typename`` still set it
        explicitly; ``file_names`` / ``chunk_start`` accept legacy
        payloads without errors even though the SDK ignores them.
        """
        input_ = TransformInput(
            typename="column",
            file_names=["batch-0.parquet", "batch-1.parquet"],
            chunk_start=2,
        )
        assert input_.typename == "column"
        assert input_.file_names == ["batch-0.parquet", "batch-1.parquet"]
        assert input_.chunk_start == 2


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
        # storage_path is pre-set to the canonical
        # ``transformed/<entity>/entities.json`` key so publish
        # discovers the file at the entity-typed path it expects.
        # Without this pin the interceptor would auto-generate a
        # ``file_refs/<uuid>.json`` key and publish would silently
        # archive the entity (production incident — see
        # ``TestCanonicalStoragePaths`` for the cross-pod regression
        # guard).
        assert result.transformed_file.storage_path is not None
        assert result.transformed_file.storage_path.endswith(
            "/transformed/table/entities.json"
        ), f"unexpected storage_path: {result.transformed_file.storage_path!r}"
        # Tier = RETAINED here because the transform → publish handoff
        # can span SDR → in-tenant deployments — the ref must survive
        # the SDR-side workflow's auto-cleanup at run end. Contrast
        # with raw_file (TRANSIENT): extract and transform always run
        # in the same deployment, so the intermediate ref is safe to
        # auto-clean.
        assert result.transformed_file.tier == StorageTier.RETAINED

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

    # ── Bug: cascading replacement (APP-2291) ────────────────────────────

    def test_no_cascade_when_exclude_regex_contains_include_placeholder(self, app):
        """Bug: chained str.replace() cascades — exclude value containing the
        literal string '{normalized_include_regex}' gets a second replacement.

        Scenario: the first str.replace() embeds '{normalized_include_regex}'
        inside the exclude value; the second str.replace() then replaces THAT
        occurrence too, silently rewriting the exclude pattern.

        Expected: the exclude value is substituted verbatim; the embedded
        placeholder text is treated as literal characters, not as a second
        substitution target.
        """
        # exclude filter whose normalized form contains the OTHER placeholder
        # string literally — only possible if a user passes it as a raw string
        # (dict path strips ^$ so the literal text is preserved).
        exclude_raw = "{normalized_include_regex}"  # a raw-string filter
        sql = (
            "WHERE schema NOT REGEXP '{normalized_exclude_regex}'"
            " AND schema REGEXP '{normalized_include_regex}'"
        )
        input_ = _make_task_input(
            exclude_filter=exclude_raw,
            include_filter="^prod$",
        )
        result = app._prepare_sql(sql, input_)

        # The exclude clause must contain the LITERAL value, not the resolved
        # include regex.  With cascading str.replace() the exclude clause would
        # be rewritten to '^prod$', making exclude == include — wrong.
        assert "NOT REGEXP '{normalized_include_regex}'" in result, (
            "exclude clause was corrupted by cascading replacement: " f"got {result!r}"
        )
        assert "REGEXP '^prod$'" in result  # include clause correct

    def test_no_cascade_when_include_regex_contains_exclude_placeholder(self, app):
        """Mirror of the above: include value containing the exclude placeholder
        must not trigger a second replace on the already-substituted text."""
        include_raw = "{normalized_exclude_regex}"
        sql = (
            "WHERE schema NOT REGEXP '{normalized_exclude_regex}'"
            " AND schema REGEXP '{normalized_include_regex}'"
        )
        input_ = _make_task_input(
            include_filter=include_raw,
            exclude_filter="^temp$",
        )
        result = app._prepare_sql(sql, input_)

        assert (
            "REGEXP '{normalized_exclude_regex}'" in result
        ), "include clause was corrupted by cascading replacement"
        assert "NOT REGEXP '^temp$'" in result

    # ── Bug: regex quantifiers survive into .format()-style templates (APP-2291) ─

    def test_regex_quantifier_preserved_and_connector_uses_replace_not_format(
        self, app
    ):
        """Regex quantifiers in substituted values are preserved in the SQL as-is
        (correct: the DB engine needs the raw {n,m} syntax).  Connectors that
        have their own placeholder substitution after _prepare_sql must use
        str.replace() rather than str.format() — .format() interprets {n,m} as
        positional argument references and raises IndexError (APP-2291).

        This test pins both halves of that contract:
          1. _prepare_sql preserves {n,m} verbatim — correct SQL behaviour.
          2. Using .replace() for the connector's own {schema_list} works safely.
        """
        include_with_quantifier = (
            "^db{2}\\.schema$"  # valid SQL-engine regex quantifier
        )

        sql = (
            "WHERE s.name IN ({schema_list})"
            " AND schema REGEXP '{normalized_include_regex}'"
        )
        input_ = _make_task_input(include_filter=include_with_quantifier)
        prepared = app._prepare_sql(sql, input_)

        # 1. The quantifier must survive verbatim so the DB engine sees it.
        assert (
            "^db{2}" in prepared
        ), "_prepare_sql must not alter regex quantifiers in substituted values"

        # 2. {schema_list} is untouched — connector can substitute it.
        assert "{schema_list}" in prepared

        # 3. Correct connector pattern: .replace(), not .format().
        final = prepared.replace("{schema_list}", "'s1', 's2'")
        assert "'s1', 's2'" in final
        assert "^db{2}" in final  # quantifier intact even after connector's own sub

        # 4. Demonstrate WHY .format() must be avoided — it raises on {2}.
        with pytest.raises((IndexError, KeyError)):
            prepared.format(schema_list="'s1', 's2'")


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
        """Return list of patches that mock all extract_* + transform_*.

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
            # Mock prime_sql_auth (BLDX-1295) — the real one opens an
            # actual SQL client. These run() tests are about output
            # plumbing, not the prime mechanism itself; the prime task
            # has its own dedicated test class below.
            patch.object(
                SqlApp,
                "prime_sql_auth",
                new=AsyncMock(return_value=PrimeAuthOutput(duration_ms=5.0)),
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


# ---------------------------------------------------------------------------
# prime_sql_auth (BLDX-1295) — Argo parity: serial probe before parallel burst
# ---------------------------------------------------------------------------


class TestPrimeSqlAuth:
    """BLDX-1295 — pre-warm SQL auth cache before parallel extract burst.

    Background:
        v3's parallel-activity execution model fans out
        ``extract_databases / extract_schemas / extract_tables /
        extract_columns`` simultaneously. Each opens its own SQL client
        and authenticates against the source — concurrently. For sources
        whose first-auth handshake is expensive or race-prone (MySQL 8's
        ``caching_sha2_password`` is the canonical example: requires
        TLS or RSA exchange when the server-side cache is cold), N
        parallel cold-cache auth attempts can each fail and stack on
        the per-user ``failed_login_attempts`` counter — tripping
        ``FAILED_LOGIN_ATTEMPTS`` lockouts on the customer's source.

        The fix is one serial probe activity, ``prime_sql_auth``,
        invoked by ``run()`` before the ``asyncio.gather(...)`` of
        extracts. One connection, one ``SELECT 1``, primes the
        server's auth cache, then parallel extracts take the fast path.

        Argo's serial extract pipeline never exposed this — only v3's
        parallel model does.
    """

    @staticmethod
    def _build_input() -> ExtractionTaskInput:
        return ExtractionTaskInput(
            workflow_id="wf-test",
            output_path="/tmp/test",
            output_prefix="/tmp",
            exclude_filter="",
            include_filter="",
            temp_table_regex="",
        )

    # ── Direct task behaviour ────────────────────────────────────────────

    async def test_prime_sql_auth_issues_select_1(self, app):
        """The probe must execute a single ``SELECT 1`` query — that's the
        whole point: a no-op query that nonetheless forces the connection
        + auth handshake, populating the server-side cache."""
        fake = FakeSQLClient(rows=[{"1": 1}])
        with patch.object(SqlApp, "_init_sql_client", new=AsyncMock(return_value=fake)):
            result = await app.prime_sql_auth(self._build_input())

        assert fake.last_query == "SELECT 1"
        assert isinstance(result, PrimeAuthOutput)
        assert result.duration_ms >= 0.0  # observability field populated

    async def test_prime_sql_auth_closes_client(self, app):
        """The probe must close its client. A leaked connection would
        defeat the priming intent — the cache is primed by *completed*
        connections, not held-open ones."""
        fake = FakeSQLClient(rows=[{"1": 1}])
        close_mock = AsyncMock(wraps=fake.close)
        fake.close = close_mock  # type: ignore[method-assign]

        with patch.object(SqlApp, "_init_sql_client", new=AsyncMock(return_value=fake)):
            await app.prime_sql_auth(self._build_input())

        close_mock.assert_awaited_once()

    async def test_prime_sql_auth_closes_client_even_on_failure(self, app):
        """When the probe query itself raises (network blip, intermittent
        auth fail), the client must still be closed so the connection
        doesn't leak — otherwise repeated failures pile up sockets.

        Note: post-review (PR #1835), prime_sql_auth catches the probe
        exception and returns ``PrimeAuthOutput(success=False, ...)``
        rather than re-raising. We still assert the inner ``client.close()``
        was awaited so the connection is released cleanly before the
        structured failure is reported upward.
        """

        class _ExplodingClient(FakeSQLClient):
            async def run_query(self, query, batch_size=100000):
                raise RuntimeError("boom")  # simulate auth/network failure

            async def get_results(self, query):
                raise RuntimeError("boom")

        fake = _ExplodingClient()
        close_mock = AsyncMock(wraps=fake.close)
        fake.close = close_mock  # type: ignore[method-assign]

        with patch.object(SqlApp, "_init_sql_client", new=AsyncMock(return_value=fake)):
            result = await app.prime_sql_auth(self._build_input())

        close_mock.assert_awaited_once()
        # Failure is now returned as data, not raised.
        assert isinstance(result, PrimeAuthOutput)
        assert result.success is False
        assert result.error_type == "RuntimeError"
        assert result.error_message is not None and "boom" in result.error_message

    async def test_prime_sql_auth_returns_failure_when_init_raises(self, app):
        """If client construction itself raises (e.g. credential resolution
        failed, host unresolvable), ``prime_sql_auth`` still reports the
        failure as structured data rather than letting Temporal retry —
        retrying that on the auth-cache probe path stacks the source's
        ``failed_login_attempts`` counter and is the exact behaviour the
        prime exists to avoid. Pinned by the PR #1835 reviewer's request."""

        async def _exploding_init(_self, _input):
            raise ConnectionError("could not resolve host 'mysql.bad'")

        with patch.object(SqlApp, "_init_sql_client", new=_exploding_init):
            result = await app.prime_sql_auth(self._build_input())

        assert result.success is False
        assert result.error_type == "ConnectionError"
        assert (
            result.error_message is not None
            and "could not resolve host" in result.error_message
        )

    async def test_prime_sql_auth_truncates_long_error_message(self, app):
        """Driver error strings can be huge (full server response dumps).
        The contract field caps at 500 chars + ellipsis so the
        ``PrimeAuthOutput`` envelope stays bounded in activity-event
        payloads."""

        long_message = "x" * 2000

        class _ExplodingClient(FakeSQLClient):
            async def get_results(self, query):
                raise RuntimeError(long_message)

        fake = _ExplodingClient()
        with patch.object(SqlApp, "_init_sql_client", new=AsyncMock(return_value=fake)):
            result = await app.prime_sql_auth(self._build_input())

        assert result.success is False
        assert result.error_message is not None
        assert len(result.error_message) == 501  # 500 chars + ellipsis
        assert result.error_message.endswith("…")

    # ── Bug-reproduction via call ordering ──────────────────────────────

    async def test_reproduces_parallel_auth_burst_when_prime_skipped(self):
        """BUG REPRO (pre-fix shape): drive the real ``SqlApp.run()`` with
        ``prime_sql_auth`` patched to a no-op, and observe ``_init_sql_client``
        being entered concurrently by the four real ``extract_*``
        activities. ``max_active >= 2`` here is meaningful — it proves
        the *SDK's* extract fan-out actually overlaps on init, which is
        exactly the cold-auth-burst race BLDX-1295 was filed for. (An
        earlier version of this test was vacuous: it awaited four bare
        helpers via ``asyncio.gather`` and never went through ``run()``.
        Pinned by application-sdk#1835 mothership comment-3287630369.)
        """
        from unittest.mock import MagicMock

        active = 0
        max_active = 0

        async def _counting_init(self_, input_):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                # Hold the client init open briefly so concurrent enters
                # are observable on the wall clock.
                await asyncio.sleep(0.02)
                return FakeSQLClient(rows=[{"x": 1}])
            finally:
                active -= 1

        async def _fake_extract(_self, input_):
            # Each real extract_* path normally goes through
            # _init_sql_client. Reproduce that here so the counter
            # observes the parallel burst — and return a minimal
            # ExtractionTaskOutput so run()'s downstream wiring
            # (transform threading) doesn't blow up.
            await _counting_init(_self, input_)
            return ExtractionTaskOutput(
                typename="x", total_record_count=0, raw_file=None
            )

        app = SqlApp.__new__(SqlApp)
        app._app_name = "test-app"

        mock_wf_info = MagicMock(workflow_id="wf-burst", run_id="run-burst")

        with (
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
            patch(
                "application_sdk.templates.sql_app._temporal_workflow.info",
                return_value=mock_wf_info,
            ),
            # Simulate the pre-fix path: prime is a no-op so it does
            # NOT serialise the first connection.
            patch.object(
                SqlApp,
                "prime_sql_auth",
                new=AsyncMock(return_value=PrimeAuthOutput(duration_ms=0.0)),
            ),
            # Real extract_* tasks each enter _counting_init — that's
            # the parallel burst we want to expose.
            patch.object(SqlApp, "extract_databases", new=_fake_extract),
            patch.object(SqlApp, "extract_schemas", new=_fake_extract),
            patch.object(SqlApp, "extract_tables", new=_fake_extract),
            patch.object(SqlApp, "extract_columns", new=_fake_extract),
            patch.object(SqlApp, "transform_databases", new=AsyncMock()),
            patch.object(SqlApp, "transform_schemas", new=AsyncMock()),
            patch.object(SqlApp, "transform_tables", new=AsyncMock()),
            patch.object(SqlApp, "transform_columns", new=AsyncMock()),
        ):
            await app.run(ExtractionInput(output_path="/tmp/test"))

        # Genuine evidence: with the prime stubbed out, the four
        # extract_* activities all entered _init_sql_client at the
        # same time. That is precisely the BLDX-1295 cold-auth burst
        # the prime_sql_auth task exists to prevent.
        assert max_active >= 2, (
            f"Expected the pre-fix shape (no real prime) to produce "
            f"overlapping _init_sql_client entries, but only saw "
            f"max_active={max_active}"
        )

    async def test_run_invokes_prime_sql_auth_before_extracts(self):
        """FIX PIN: ``SqlApp.run()`` must call ``prime_sql_auth`` exactly
        once, before any ``extract_*`` activity fires. Without this
        ordering, the parallel extract burst hits cold-cache and can
        trigger the lockout cycle. With it, the cache is primed once
        and parallel extracts take the fast path."""
        app = SqlApp.__new__(SqlApp)
        app._app_name = "test-app"

        call_order: list[str] = []

        def _record_prime(_inp):
            call_order.append("prime_sql_auth")
            return PrimeAuthOutput(duration_ms=1.0)

        def _make_extract_recorder(name: str):
            def _record(_inp):
                call_order.append(name)
                return ExtractionTaskOutput(
                    typename=name.removeprefix("extract_"),
                    total_record_count=1,
                    raw_file=None,
                )

            return _record

        mock_wf_info = MagicMock(workflow_id="wf-1", run_id="run-1")

        with (
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
            patch(
                "application_sdk.templates.sql_app._temporal_workflow.info",
                return_value=mock_wf_info,
            ),
            patch.object(
                SqlApp,
                "prime_sql_auth",
                new=AsyncMock(side_effect=_record_prime),
            ),
            patch.object(
                SqlApp,
                "extract_databases",
                new=AsyncMock(side_effect=_make_extract_recorder("extract_databases")),
            ),
            patch.object(
                SqlApp,
                "extract_schemas",
                new=AsyncMock(side_effect=_make_extract_recorder("extract_schemas")),
            ),
            patch.object(
                SqlApp,
                "extract_tables",
                new=AsyncMock(side_effect=_make_extract_recorder("extract_tables")),
            ),
            patch.object(
                SqlApp,
                "extract_columns",
                new=AsyncMock(side_effect=_make_extract_recorder("extract_columns")),
            ),
            patch.object(SqlApp, "transform_databases", new=AsyncMock()),
            patch.object(SqlApp, "transform_schemas", new=AsyncMock()),
            patch.object(SqlApp, "transform_tables", new=AsyncMock()),
            patch.object(SqlApp, "transform_columns", new=AsyncMock()),
        ):
            await app.run(ExtractionInput(output_path="/tmp/test"))

        assert (
            call_order[0] == "prime_sql_auth"
        ), f"prime_sql_auth must run first; got order: {call_order}"
        assert (
            call_order.count("prime_sql_auth") == 1
        ), f"prime_sql_auth must run exactly once; got order: {call_order}"
        for ext in (
            "extract_databases",
            "extract_schemas",
            "extract_tables",
            "extract_columns",
        ):
            assert (
                call_order.index(ext) > 0
            ), f"{ext} must run after prime_sql_auth; got order: {call_order}"

    async def test_prime_failure_short_circuits_run(self):
        """When ``prime_sql_auth`` reports ``success=False`` (credential
        failure, network unreachable), ``run()`` must abort BEFORE the
        parallel extract burst. Otherwise we'd flood the source with N
        parallel auth-failure attempts — the exact behaviour the prime
        is meant to prevent.

        Post-review (PR #1835): prime returns structured failure
        instead of raising. ``run()`` translates it into a typed
        ``AuthError`` that names the prime step + the underlying
        driver error + a recommended fix path.
        """
        from application_sdk.errors.leaves import AuthError

        app = SqlApp.__new__(SqlApp)
        app._app_name = "test-app"

        extract_called = AsyncMock(
            return_value=ExtractionTaskOutput(
                typename="db", total_record_count=0, raw_file=None
            )
        )

        with (
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
            patch.object(
                SqlApp,
                "prime_sql_auth",
                new=AsyncMock(
                    return_value=PrimeAuthOutput(
                        duration_ms=12.3,
                        success=False,
                        error_type="OperationalError",
                        error_message="(1045, \"Access denied for user 'foo'@'1.2.3.4'\")",
                    ),
                ),
            ),
            patch.object(SqlApp, "extract_databases", new=extract_called),
            patch.object(SqlApp, "extract_schemas", new=extract_called),
            patch.object(SqlApp, "extract_tables", new=extract_called),
            patch.object(SqlApp, "extract_columns", new=extract_called),
        ):
            with pytest.raises(AuthError) as exc_info:
                await app.run(ExtractionInput(output_path="/tmp/test"))

        extract_called.assert_not_called()

        # The raised AuthError must carry actionable context — the prime
        # step is named, the underlying driver error is surfaced as
        # failure_reason, and the suggestion mentions FAILED_LOGIN_ATTEMPTS
        # so an operator knows what to check before retrying.
        err = exc_info.value
        assert "rejected by source" in err.message.lower()
        assert "OperationalError" in err.message
        assert err.failure_reason and "Access denied" in err.failure_reason
        assert err.auth_method == "sql_client"
        assert err.suggested_action and "FAILED_LOGIN_ATTEMPTS" in err.suggested_action

    async def test_prime_failure_yields_auth_error_with_no_temporal_retry(self):
        """Pin the BLDX-1295 retry-semantics decision: the structured
        AuthError raised by ``run()`` on prime failure is
        ``effective_retryable=False``. This is what stops Temporal from
        auto-retrying the run-level activity and stacking the source's
        ``failed_login_attempts`` counter on each retry — the original
        ``@task(retry_max_attempts=3)`` on the prime itself was the
        bug; this test pins that we don't reintroduce auto-retry on the
        failure path.
        """
        from application_sdk.errors.leaves import AuthError

        app = SqlApp.__new__(SqlApp)
        app._app_name = "test-app"

        with (
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
            patch.object(
                SqlApp,
                "prime_sql_auth",
                new=AsyncMock(
                    return_value=PrimeAuthOutput(
                        duration_ms=10.0,
                        success=False,
                        error_type="OperationalError",
                        error_message="Access denied",
                    ),
                ),
            ),
            patch.object(SqlApp, "extract_databases", new=AsyncMock()),
            patch.object(SqlApp, "extract_schemas", new=AsyncMock()),
            patch.object(SqlApp, "extract_tables", new=AsyncMock()),
            patch.object(SqlApp, "extract_columns", new=AsyncMock()),
        ):
            with pytest.raises(AuthError) as exc_info:
                await app.run(ExtractionInput(output_path="/tmp/test"))

        assert exc_info.value.effective_retryable is False

    @pytest.mark.parametrize(
        "error_type,error_message",
        [
            ("TimeoutError", "operation timed out after 60s"),
            ("AsyncioTimeoutError", "deadline exceeded"),
            ("OperationalError", "(2013) Lost connection during query — timed out"),
        ],
    )
    async def test_prime_timeout_raises_app_timeout_error(
        self, error_type, error_message
    ):
        """Probe failures classified as timeouts must surface as
        ``AppTimeoutError`` — NOT ``AuthError`` — so the on-call's
        ``suggested_action`` is "check network reachability" rather
        than "run ACCOUNT UNLOCK". Pinned by application-sdk#1835
        mothership comment-3287630230 (HIGH).
        """
        from application_sdk.errors.leaves import AppTimeoutError

        app = SqlApp.__new__(SqlApp)
        app._app_name = "test-app"

        with (
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
            patch.object(
                SqlApp,
                "prime_sql_auth",
                new=AsyncMock(
                    return_value=PrimeAuthOutput(
                        duration_ms=60_000.0,
                        success=False,
                        error_type=error_type,
                        error_message=error_message,
                    ),
                ),
            ),
            patch.object(SqlApp, "extract_databases", new=AsyncMock()),
            patch.object(SqlApp, "extract_schemas", new=AsyncMock()),
            patch.object(SqlApp, "extract_tables", new=AsyncMock()),
            patch.object(SqlApp, "extract_columns", new=AsyncMock()),
        ):
            with pytest.raises(AppTimeoutError) as exc_info:
                await app.run(ExtractionInput(output_path="/tmp/test"))

        err = exc_info.value
        assert err.operation == "prime_sql_auth"
        # AppTimeoutError doesn't define a structured ``failure_reason``
        # field — driver message is inlined into ``message`` instead.
        assert error_message in err.message
        assert err.suggested_action and "network reachability" in err.suggested_action

    @pytest.mark.parametrize(
        "error_type,error_message",
        [
            ("ConnectionRefusedError", "[Errno 111] Connection refused"),
            ("gaierror", "[Errno -2] Name or service not known"),
            ("SSLError", "[SSL: CERTIFICATE_VERIFY_FAILED]"),
            (
                "OperationalError",
                "(2003) Can't connect to MySQL server on 'db.example.com'",
            ),
        ],
    )
    async def test_prime_network_failure_raises_dependency_unavailable(
        self, error_type, error_message
    ):
        """Probe failures that look like network / DNS / TLS /
        connection-refused must surface as
        ``DependencyUnavailableError``. The on-call guidance must NOT
        suggest a credentials check (the credentials path was never
        exercised) and must NOT suggest ACCOUNT UNLOCK (no failed
        login attempts were made). Pinned by application-sdk#1835
        mothership comment-3287630230 (HIGH).
        """
        from application_sdk.errors.leaves import DependencyUnavailableError

        app = SqlApp.__new__(SqlApp)
        app._app_name = "test-app"

        with (
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
            patch.object(
                SqlApp,
                "prime_sql_auth",
                new=AsyncMock(
                    return_value=PrimeAuthOutput(
                        duration_ms=50.0,
                        success=False,
                        error_type=error_type,
                        error_message=error_message,
                    ),
                ),
            ),
            patch.object(SqlApp, "extract_databases", new=AsyncMock()),
            patch.object(SqlApp, "extract_schemas", new=AsyncMock()),
            patch.object(SqlApp, "extract_tables", new=AsyncMock()),
            patch.object(SqlApp, "extract_columns", new=AsyncMock()),
        ):
            with pytest.raises(DependencyUnavailableError) as exc_info:
                await app.run(ExtractionInput(output_path="/tmp/test"))

        err = exc_info.value
        assert err.service == "sql_source"
        assert err.network_error == error_message
        # Must steer the operator toward network/DNS investigation, and
        # explicitly NOT toward auth-lockout work. The classifier's
        # actual phrasing is "this is not a lockout situation — do not
        # run ACCOUNT UNLOCK" (a helpful disclaimer), so check the
        # positive guidance is network-shaped.
        assert err.suggested_action and "DNS" in err.suggested_action
        assert (
            err.suggested_action and "FAILED_LOGIN_ATTEMPTS" not in err.suggested_action
        )

    async def test_prime_unknown_error_class_falls_back_to_internal(self):
        """Last-resort: a driver throws an unrecognised exception class.
        The classifier must NOT swallow it as auth — it returns
        ``InternalError`` so the on-call sees "unclassified" and knows
        to inspect worker logs (where ``exc_info=True`` preserves the
        full traceback). Pinned by application-sdk#1835 mothership
        comment-3287630230 (HIGH).
        """
        from application_sdk.errors.leaves import InternalError

        app = SqlApp.__new__(SqlApp)
        app._app_name = "test-app"

        with (
            patch.object(SqlApp, "_resolve_credential_ref", return_value=None),
            patch.object(
                SqlApp,
                "prime_sql_auth",
                new=AsyncMock(
                    return_value=PrimeAuthOutput(
                        duration_ms=10.0,
                        success=False,
                        error_type="WeirdVendorSpecificError",
                        error_message="something we have never seen before",
                    ),
                ),
            ),
            patch.object(SqlApp, "extract_databases", new=AsyncMock()),
            patch.object(SqlApp, "extract_schemas", new=AsyncMock()),
            patch.object(SqlApp, "extract_tables", new=AsyncMock()),
            patch.object(SqlApp, "extract_columns", new=AsyncMock()),
        ):
            with pytest.raises(InternalError) as exc_info:
                await app.run(ExtractionInput(output_path="/tmp/test"))

        err = exc_info.value
        assert "unclassified" in err.message.lower()
        assert (
            err.suggested_action and "_classify_prime_failure" in err.suggested_action
        )
