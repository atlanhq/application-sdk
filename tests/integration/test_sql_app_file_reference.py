"""Integration tests for the SqlApp FileReference handshake (BLDX-1281).

Verifies the full extract → persist → cross-worker materialize → transform
flow against a real local obstore (no mocks, no Temporal). Specifically
pins the cross-worker fault tolerance that motivated this fix:

  1. ``extract_*`` writes ``raw/<entity>/records.json`` locally and
     returns an ephemeral ``FileReference``.
  2. The persistence layer uploads the ref to the store and marks it
     durable (with a SHA-256 sidecar) — this is what the activity
     interceptor does after the extract activity finishes.
  3. Simulate the transform landing on a different worker by deleting
     the original local file. ``materialize_file_reference`` re-downloads
     from the store and verifies the sidecar.
  4. ``transform_*`` runs against the materialized FileReference,
     reading from its ``local_path`` (which points to the freshly-
     downloaded copy) — NOT the original local path.
  5. The emitted ``transformed_file`` ref is persisted and we confirm
     the entities.json landed in the store with its own sidecar.

If any of these steps regresses, a customer with >1 worker replica
risks the same silent-archive incident this PR was filed to fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.contracts.types import FileReference
from application_sdk.storage.factory import create_local_store
from application_sdk.storage.reference import (
    materialize_file_reference,
    persist_file_reference,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionTaskInput,
    TransformInput,
)
from application_sdk.templates.sql_app import SqlApp


class _FakeSqlClient(BaseSQLClient):
    """In-process SQL client that yields a fixed batch — no network, no driver.

    Mirrors the streaming contract ``SqlApp._extract_entity`` relies on
    (``run_query`` yields ``list[dict]`` batches). We bypass ``BaseSQLClient``
    initialisation because we never go through ``client.load()`` —
    ``_init_sql_client`` is patched out below.
    """

    _ROWS: ClassVar[list[dict[str, Any]]] = [
        {"database_name": "prod"},
        {"database_name": "stage"},
    ]

    def __init__(self) -> None:
        pass  # skip BaseSQLClient.__init__ — no DB_CONFIG needed for the fake

    async def load(self, credentials: dict[str, Any] | None = None) -> None:
        return None

    async def close(self) -> None:
        return None

    async def run_query(self, query: str, batch_size: int = 100_000):
        yield self._ROWS


class _ConcreteSqlApp(SqlApp):
    """Minimal SqlApp subclass exercising the extract → transform flow."""

    sql_client_class: ClassVar[type[BaseSQLClient] | None] = _FakeSqlClient
    _app_registered: ClassVar[bool] = True

    fetch_database_sql: ClassVar[str] = "SELECT 1"

    def map_database(
        self, record: dict[str, Any], connection_qn: str
    ) -> dict[str, Any]:
        return {
            "typeName": "Database",
            "attributes": {
                "name": record["database_name"],
                "qualifiedName": f"{connection_qn}/{record['database_name']}",
            },
        }


@pytest.fixture
def store(tmp_path):
    """Real local object store backed by a temp directory."""
    return create_local_store(tmp_path / "store")


def _make_extract_input(tmp_path: Path) -> ExtractionTaskInput:
    """Build an ``ExtractionTaskInput`` for the extract side (no raw_file)."""
    return ExtractionTaskInput(
        workflow_id="wf-test",
        output_path=str(tmp_path),
        output_prefix=str(tmp_path),
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
    )


def _make_transform_input(
    tmp_path: Path, raw_file: FileReference | None = None
) -> TransformInput:
    """Build a ``TransformInput`` carrying the materialised ``raw_file``."""
    return TransformInput(
        workflow_id="wf-test",
        output_path=str(tmp_path),
        output_prefix=str(tmp_path),
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
        raw_file=raw_file,
    )


# ---------------------------------------------------------------------------
# Full extract → persist → cross-worker materialize → transform → persist flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_extract_persist_materialize_transform_round_trip(store, tmp_path):
    """End-to-end fault-tolerance check against a real object store.

    Reproduces the production scenario this fix exists for: extract
    runs on one worker, transform runs on a different worker (raw file
    missing locally), the framework's persist + materialize handshake
    recovers, and transform emits its own durable ref for publish.
    """
    app = _ConcreteSqlApp()

    # ── Step 1: extract → ephemeral raw_file FileReference ─────────────
    extract_input = _make_extract_input(tmp_path)
    extract_result = await app.extract_databases(extract_input)

    assert extract_result.total_record_count == 2
    assert extract_result.raw_file is not None
    ephemeral_raw = extract_result.raw_file
    assert ephemeral_raw.is_durable is False
    # storage_path is now pre-set to the canonical key (no longer
    # None pre-persist) — see ``_extract_entity``'s emission site for
    # why we pin the key here. The interceptor's persist honours it
    # instead of auto-generating a ``file_refs/<uuid>`` orphan.
    assert ephemeral_raw.storage_path is not None
    assert ephemeral_raw.storage_path.endswith("/raw/database/records.json")
    assert ephemeral_raw.local_path == str(
        tmp_path / "raw" / "database" / "records.json"
    )
    assert Path(ephemeral_raw.local_path).exists()

    # ── Step 2: persist → durable raw_file ref (what the interceptor does) ─
    # The pre-set storage_path on the ref is what persist actually
    # uses as the upload key, so the file lands at the canonical
    # entity-typed path (matches what ``upload_to_atlan``'s legacy
    # directory walk would have produced — publish discovers it the
    # same way).
    durable_raw = await persist_file_reference(
        store, ephemeral_raw, output_path=str(tmp_path)
    )
    assert durable_raw.is_durable
    assert durable_raw.storage_path is not None
    # Storage key is the canonical entity-typed path — NOT under
    # ``file_refs/<uuid>``. The customer-bucket repro had the
    # ``file_refs/<uuid>`` orphan; we explicitly verify we don't
    # produce one.
    assert "/file_refs/" not in durable_raw.storage_path, (
        f"raw_file landed at file_refs/ orphan instead of canonical "
        f"transformed/<entity>/ key: {durable_raw.storage_path}"
    )
    assert durable_raw.storage_path.endswith(
        "/raw/database/records.json"
    ), f"raw_file did not land at canonical key: {durable_raw.storage_path}"

    # ── Step 3: simulate cross-worker — local raw file vanishes ─────────
    # This mimics transform landing on a different Temporal worker pod
    # that never ran the matching extract. Pre-fix, _transform_entity
    # would see the missing file and silently return count=0.
    Path(ephemeral_raw.local_path).unlink()
    sidecar_local = Path(ephemeral_raw.local_path + ".sha256")
    if sidecar_local.exists():
        sidecar_local.unlink()

    # ── Step 4: materialize on the "new" worker (fresh download) ───────
    # The framework's interceptor calls this automatically before the
    # transform activity runs; we invoke it directly here to exercise
    # the contract without spinning Temporal.
    materialized_raw = await materialize_file_reference(store, durable_raw)
    assert materialized_raw.local_path is not None
    assert Path(materialized_raw.local_path).exists(), (
        "materialize must produce a local copy of the raw file even when "
        "the extract-worker's original local path is gone — this is the "
        "core of the cross-worker fix"
    )

    # ── Step 5: transform reads via input.raw_file.local_path ──────────
    transform_input = _make_transform_input(tmp_path, raw_file=materialized_raw)
    transform_result = await app.transform_databases(transform_input)

    assert transform_result.total_record_count == 2
    assert transform_result.transformed_file is not None
    assert transform_result.transformed_file.local_path == str(
        tmp_path / "transformed" / "database" / "entities.json"
    )

    # Inspect entities.json contents — both rows must be mapped, regardless
    # of which worker pod the transform happened to run on.
    out = Path(transform_result.transformed_file.local_path).read_text()
    rendered = [json.loads(line) for line in out.strip().split("\n")]
    assert {r["attributes"]["name"] for r in rendered} == {"prod", "stage"}

    # ── Step 6: persist transformed_file ref → downstream consumes durable ─
    # The transformed ref also carries a pre-set canonical
    # storage_path (``transformed/<entity>/entities.json``) so persist
    # uploads to where publish reads from — no ``file_refs/<uuid>``
    # orphan, no dependence on ``upload_to_atlan`` walking local FS
    # on a different pod than this transform.
    assert transform_result.transformed_file.storage_path is not None
    assert transform_result.transformed_file.storage_path.endswith(
        "/transformed/database/entities.json"
    )
    durable_transformed = await persist_file_reference(
        store, transform_result.transformed_file, output_path=str(tmp_path)
    )
    assert durable_transformed.is_durable
    assert durable_transformed.storage_path is not None
    assert "/file_refs/" not in durable_transformed.storage_path
    assert durable_transformed.storage_path.endswith(
        "/transformed/database/entities.json"
    )


@pytest.mark.integration
async def test_materialize_after_extract_pod_local_path_stale(store, tmp_path):
    """SHA-256 sidecar verification path: stale ``local_path`` that points
    to a file that was deleted (or never existed on this worker) must
    NOT short-circuit the download — the file_ref_sync docstring at
    line 28 calls out this exact case as the cross-worker invariant.
    """
    # Set up a raw file + persist to store.
    raw_local = tmp_path / "raw" / "database" / "records.json"
    raw_local.parent.mkdir(parents=True, exist_ok=True)
    raw_local.write_text(json.dumps({"database_name": "x"}) + "\n")

    ephemeral = FileReference(local_path=str(raw_local))
    durable = await persist_file_reference(store, ephemeral)

    # Simulate the "transform on different worker" environment by
    # deleting both the file and its local sidecar. The durable ref
    # still carries the stale local_path string.
    raw_local.unlink()
    sidecar = Path(str(raw_local) + ".sha256")
    if sidecar.exists():
        sidecar.unlink()

    # Materialize must rebuild the local file from the store.
    materialized = await materialize_file_reference(store, durable)
    assert materialized.local_path is not None
    assert Path(materialized.local_path).exists()
    assert (
        json.loads(Path(materialized.local_path).read_text().strip())["database_name"]
        == "x"
    )


@pytest.mark.integration
async def test_prod_regression_cross_worker_transform_recovers(store, tmp_path):
    """Regression: pin the exact production scenario this fix exists for.

    Production timeline (a SQL connector running v3.10.0 of the SDK):
        1. ``extract_schemas`` ran on worker pod A, wrote
           ``raw/schema/records.json`` to pod A's local FS, and the
           SDK auto-uploaded it to S3.
        2. ``transform_schemas`` ran on worker pod B (different replica).
           Pod B's local FS had no raw file — the OLD code returned
           ``TransformOutput(total_record_count=0)`` silently.
        3. The publish step then archived all 14 schemas for the
           connection because the transformed directory was empty.

    This test:
        * Reproduces pod A's extract output (a raw file + a durable ref
          in the object store).
        * Simulates the pod B environment by deleting pod A's local file.
        * Confirms that, BEFORE the fix, transform would have returned 0
          (the legacy fallback path with raw_file=None).
        * Confirms that, AFTER the fix, threading the durable ref into
          ``TransformInput.raw_file`` lets the interceptor materialise it
          on pod B and the transform processes the records correctly.

    If this test ever regresses, the silent-archive behaviour returns
    and customers running multi-replica workers risk losing data on
    every cron run that hits the wrong pod-scheduling combo.
    """
    # Mimic a real extract output: 14 schemas in raw/schema/records.json
    # (count matches the production incident the fix was filed against).
    schema_names = [f"schema_{i}" for i in range(14)]

    class _SchemaApp(SqlApp):
        sql_client_class: ClassVar[type[BaseSQLClient] | None] = None
        _app_registered: ClassVar[bool] = True

        def map_schema(self, record, connection_qn):
            return {
                "typeName": "Schema",
                "attributes": {"name": record["schema_name"]},
            }

    pod_a_dir = tmp_path / "pod-a"
    raw_dir = pod_a_dir / "raw" / "schema"
    raw_dir.mkdir(parents=True)
    raw_local = raw_dir / "records.json"
    raw_local.write_text(
        "\n".join(json.dumps({"schema_name": name}) for name in schema_names) + "\n"
    )

    # The extract activity already finished and persisted the raw file —
    # mirror what the interceptor would have done after extract returned.
    ephemeral = FileReference(local_path=str(raw_local))
    durable = await persist_file_reference(store, ephemeral)
    assert durable.is_durable

    # ─── Simulate pod B: nothing on local FS that pod A produced ────────
    raw_local.unlink()
    sidecar = Path(str(raw_local) + ".sha256")
    if sidecar.exists():
        sidecar.unlink()
    # Different output_path too (pod B is a different worker, different
    # tmp dir mount). Pod B's transform CANNOT find raw_local at all.
    pod_b_dir = tmp_path / "pod-b"
    pod_b_dir.mkdir()

    app = _SchemaApp()

    # ─── BEFORE the fix: transform with NO raw_file ref → silent zero ──
    # This is the historical behaviour that caused the archive incident.
    pre_fix_input = TransformInput(
        workflow_id="wf-test",
        output_path=str(pod_b_dir),
        output_prefix=str(pod_b_dir),
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
        raw_file=None,  # ← what the old run() effectively passed
    )
    pre_fix_result = await app.transform_schemas(pre_fix_input)
    assert pre_fix_result.total_record_count == 0, (
        "regression check: with NO raw_file ref + missing local file, the "
        "old silent-zero behaviour is reproduced — this is the exact "
        "shape that archived the customer's schemas"
    )
    assert not (pod_b_dir / "transformed" / "schema" / "entities.json").exists(), (
        "regression check: no entities.json must be produced — that's "
        "what the downstream publish step interpreted as 'this entity "
        "is gone' on the impacted production tenant"
    )

    # ─── AFTER the fix: thread the durable raw_file ref → transform works ─
    # The interceptor would call materialize_file_reference for us at
    # activity-boundary time; we do it inline to keep the test pure.
    materialised = await materialize_file_reference(store, durable)

    post_fix_input = TransformInput(
        workflow_id="wf-test",
        output_path=str(pod_b_dir),
        output_prefix=str(pod_b_dir),
        exclude_filter="",
        include_filter="",
        temp_table_regex="",
        raw_file=materialised,  # ← what run() now threads via _build_transform_input
    )
    post_fix_result = await app.transform_schemas(post_fix_input)

    assert post_fix_result.total_record_count == 14, (
        "post-fix: transform must process ALL 14 schemas even though "
        "the extract-worker's local file is gone"
    )
    entities_path = pod_b_dir / "transformed" / "schema" / "entities.json"
    assert entities_path.exists()
    rendered = [
        json.loads(line) for line in entities_path.read_text().strip().split("\n")
    ]
    assert {e["attributes"]["name"] for e in rendered} == set(schema_names), (
        "post-fix: every schema name from the extract pod must surface "
        "in the transform output — no silent drops"
    )
    assert post_fix_result.transformed_file is not None, (
        "post-fix: transform must emit a durable handle to entities.json "
        "so the publish step consumes the same FileReference contract"
    )


@pytest.mark.integration
async def test_zero_row_extract_emits_no_raw_file(store, tmp_path):
    """Genuine zero-row extract must NOT emit a raw_file ref. Without
    this, the persist step would upload an empty file and the downstream
    transform would still see a durable ref (just to an empty object) —
    breaking the historical 'extract returned 0 rows' signal that
    publish relies on to distinguish empty extracts from cross-worker
    misses.
    """

    class _EmptyClient(_FakeSqlClient):
        _ROWS: ClassVar[list[dict[str, Any]]] = []

    class _EmptyApp(_ConcreteSqlApp):
        sql_client_class: ClassVar[type[BaseSQLClient] | None] = _EmptyClient

    app = _EmptyApp()
    result = await app.extract_databases(_make_extract_input(tmp_path))

    assert result.total_record_count == 0
    assert result.raw_file is None


# ---------------------------------------------------------------------------
# Cross-worker local GC: materialize into run-scoped scratch, then reclaim it
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_local_gc_reclaims_finished_run_scratch(store, tmp_path):
    """A worker materialises a durable ref into its run-scoped scratch; a later
    sweep reclaims that scratch once Temporal reports the run terminal, while a
    still-RUNNING run's scratch survives.

    Exercises the real chain end-to-end against a local obstore — ``run_scoped_dir``
    → ``materialize_file_reference(local_dir=...)`` → ``sweep_local_file_refs`` —
    with only the Temporal client faked (consistent with this module's no-Temporal
    approach). This is the cross-worker GC contract: a worker reclaims *another*
    run's local copy by querying the authoritative run status.
    """
    from temporalio.client import WorkflowExecutionStatus

    from application_sdk.storage.local_gc import run_scoped_dir, sweep_local_file_refs

    # Upload a local file so we have a durable ref to materialise.
    src = tmp_path / "io_pairs.json"
    src.write_text(json.dumps([{"q": "select 1"}]))
    durable = await persist_file_reference(
        store, FileReference(local_path=str(src), is_durable=False)
    )
    assert durable.storage_path is not None

    # "Worker B" materialises it into the run-scoped scratch for a finished run.
    root = tmp_path / "file_refs_local"
    done_dir = run_scoped_dir("wf-gc", "run-done", root=str(root))
    materialised = await materialize_file_reference(
        store,
        FileReference(
            is_durable=True, storage_path=durable.storage_path, local_path=None
        ),
        local_dir=str(done_dir),
    )
    assert materialised.local_path is not None
    assert materialised.local_path.startswith(str(done_dir))
    assert done_dir.exists()

    # A second run is still RUNNING and has its own scratch.
    live_dir = run_scoped_dir("wf-gc", "run-live", root=str(root))
    live_dir.mkdir(parents=True)
    (live_dir / "io_pairs.json").write_text("[]")

    # A third worker sweeps: describe() drives delete (terminal) vs keep (running).
    class _FakeHandle:
        def __init__(self, status: WorkflowExecutionStatus) -> None:
            self._status = status

        async def describe(self) -> Any:
            return type("Desc", (), {"status": self._status})()

    class _FakeClient:
        def __init__(self, by_run: dict[str, WorkflowExecutionStatus]) -> None:
            self._by_run = by_run

        def get_workflow_handle(
            self, workflow_id: str, *, run_id: str | None = None
        ) -> _FakeHandle:
            return _FakeHandle(self._by_run[run_id])

    client = _FakeClient(
        {
            "run-done": WorkflowExecutionStatus.COMPLETED,
            "run-live": WorkflowExecutionStatus.RUNNING,
        }
    )
    result = await sweep_local_file_refs(
        client, current_run_id="run-other", max_describes=50, root=str(root)
    )

    assert not done_dir.exists()  # terminal run's scratch reclaimed
    assert live_dir.exists()  # running run's scratch preserved
    assert str(done_dir) in result.deleted
    assert str(live_dir) in result.kept
