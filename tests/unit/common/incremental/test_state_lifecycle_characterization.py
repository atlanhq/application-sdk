"""Regression tests for the incremental-state lifecycle bugs FND-3061 pinned.

Each test here reproduces one latent bug in the pre-FND-3064 incremental state
handling. They were written as ``xfail(strict=True)`` characterizations and
flipped to plain tests when FND-3064's ``CurrentStateStore`` (manifest commit,
run-scoped state directories) fixed them; #5, #6 and #8 were flipped earlier by
FND-3063 (not-found handling, backfill wiring, blocking walks).

Setup checks go through ``_require`` (``pytest.fail``) rather than ``assert`` so
a broken setup reads as a setup failure, not as the bug.

Scenario #1 (stale-key accumulation across two runs) lives in
``tests/integration/test_incremental_pipeline.py`` alongside the other
multi-run pipeline simulations.

State trees run against a real ``LocalStore``; only failure injection is
mocked.
"""

from __future__ import annotations

import asyncio
import json
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from obstore.store import LocalStore

from application_sdk.app.context import AppContext
from application_sdk.common._listing import has_internal_component
from application_sdk.common.incremental import helpers
from application_sdk.common.incremental.marker import fetch_marker_from_storage
from application_sdk.common.incremental.state.state_writer import (
    create_current_state_snapshot,
    materialize_previous_state,
)
from application_sdk.common.incremental.state.store import (
    CurrentStateStore,
    RunStateDirs,
)
from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef
from application_sdk.infrastructure.context import (
    InfrastructureContext,
    clear_infrastructure,
    set_infrastructure,
)
from application_sdk.storage.batch import upload_prefix
from application_sdk.storage.errors import StorageError
from application_sdk.storage.factory import create_local_store
from application_sdk.templates.contracts.incremental_sql import (
    IncrementalRunContext,
    PrepareColumnQueriesInput,
    ReadCurrentStateInput,
    WriteCurrentStateInput,
)
from application_sdk.templates.incremental_sql_metadata_extractor import (
    IncrementalSqlMetadataExtractor,
)

CONN_QN = "default/example/1700000000"
APP = "example-app"
STATE_PREFIX = f"persistent-artifacts/apps/{APP}/connection/1700000000/current-state"
DB = f"{CONN_QN}/EXAMPLE_DB"
SCHEMA = f"{DB}/EXAMPLE_SCHEMA"
T1 = f"{SCHEMA}/TABLE_ONE"
T2 = f"{SCHEMA}/TABLE_TWO"


# ---------------------------------------------------------------------------
# Entity builders
# ---------------------------------------------------------------------------


def _table(qn: str, state: str = "NO CHANGE", *, run: str = "") -> dict:
    return {
        "typeName": "Table",
        "status": "ACTIVE",
        "attributes": {
            "qualifiedName": qn,
            "name": qn.rsplit("/", 1)[-1],
            "databaseName": "EXAMPLE_DB",
            "schemaName": "EXAMPLE_SCHEMA",
            "lastSyncRun": run,
        },
        "customAttributes": json.dumps({"incremental_state": state}),
    }


def _column(table_qn: str, name: str, *, run: str = "") -> dict:
    return {
        "typeName": "Column",
        "status": "ACTIVE",
        "attributes": {
            "qualifiedName": f"{table_qn}/{name}",
            "name": name,
            "tableQualifiedName": table_qn,
            "lastSyncRun": run,
        },
        "customAttributes": "{}",
    }


def _write_jsonl(path: Path, entities: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entities), encoding="utf-8")


def _tree(root: Path) -> dict[str, bytes]:
    """Every data file under *root*, keyed by its path relative to *root*.

    SDK working directories (a sync's ``.sdk-sync`` index, staging dirs) are
    bookkeeping, not state, so they are left out.
    """
    return {
        rel: p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and not has_internal_component(rel := p.relative_to(root).as_posix())
    }


def _store() -> CurrentStateStore:
    return CurrentStateStore.for_connection(CONN_QN, APP)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    """A real LocalStore bound as the infrastructure store."""
    local = create_local_store(tmp_path / "store")
    set_infrastructure(InfrastructureContext(storage=local))
    try:
        yield local
    finally:
        clear_infrastructure()


@pytest.fixture
def staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the persistent-artifacts local root at a per-test directory.

    ``helpers`` binds ``TEMPORARY_PATH`` at import, so patching
    ``constants.TEMPORARY_PATH`` alone would not move it.
    """
    root = tmp_path / "staging"
    root.mkdir()
    monkeypatch.setattr(helpers, "TEMPORARY_PATH", str(root))
    return root


async def _seed_state(tmp_path: Path, files: dict[str, list[dict]]) -> Path:
    """Upload a committed current-state snapshot to the store; return its source."""
    src = tmp_path / "seed-state"
    for rel, entities in files.items():
        _write_jsonl(src / rel, entities)
    await upload_prefix(local_dir=str(src), prefix=STATE_PREFIX)
    return src


with warnings.catch_warnings():
    # The template is deprecated for new connectors, but it is the code path
    # these bugs live in; the subclass warning is not what is under test.
    warnings.simplefilter("ignore", DeprecationWarning)

    class _Extractor(IncrementalSqlMetadataExtractor):
        def build_incremental_column_sql(
            self, table_ids: list[str], ctx: IncrementalRunContext
        ) -> str:
            return "SELECT 1"


def _require(condition: bool, precondition: str) -> None:
    """Fail as a setup failure when a test's precondition did not hold."""
    if not condition:
        pytest.fail(f"precondition: {precondition}")


def _extractor(run_id: str = "run") -> _Extractor:
    """An extractor instance without App registration (tasks are plain calls).

    *run_id* stands in for the Temporal run ID the worker puts on the app
    context, which ``write_current_state`` stamps and keys by.
    """
    extractor = _Extractor.__new__(_Extractor)
    extractor._context = AppContext(
        app_name=APP, app_version="0.1.0", run_id=run_id, workflow_id="wf"
    )
    return extractor


# ---------------------------------------------------------------------------
# #2 — partial upload failure, then a retry
# ---------------------------------------------------------------------------


async def test_retry_after_partial_upload_sees_a_consistent_previous_state(
    tmp_path: Path, store: LocalStore, staging: Path
) -> None:
    run1 = await _seed_state(
        tmp_path,
        {
            "table/chunk-0.json": [_table(T1, run="run-1"), _table(T2, run="run-1")],
            "column/chunk-0.json": [_column(T1, "A", run="run-1")],
            "column/chunk-1.json": [_column(T2, "B", run="run-1")],
        },
    )
    committed = _tree(run1)

    # Run 2 rewrites every file under the same names, so any mix of run 1 and
    # run 2 bytes in the store is detectable per file.
    transformed = tmp_path / "run-2" / "transformed"
    _write_jsonl(
        transformed / "table" / "chunk-0.json",
        [_table(T1, "UPDATED", run="run-2"), _table(T2, "UPDATED", run="run-2")],
    )
    _write_jsonl(
        transformed / "column" / "chunk-0.json", [_column(T1, "A", run="run-2")]
    )
    _write_jsonl(
        transformed / "column" / "chunk-1.json", [_column(T2, "B", run="run-2")]
    )

    dirs = RunStateDirs.for_output_path(tmp_path / "run-2")
    previous = await materialize_previous_state(_store(), dirs.previous_state)

    # The first current-state object lands, every later one fails: the shape
    # of a pod killed or a store outage part-way through step 6.
    from application_sdk.storage import batch

    real_upload_file = batch.upload_file
    landed: list[str] = []

    async def _fail_after_first(key, path, *args, **kwargs):
        if "/current-state/" in key:
            if landed:
                raise StorageError(f"injected upload failure: {key}")
            landed.append(key)
        return await real_upload_file(key, path, *args, **kwargs)

    with patch.object(batch, "upload_file", _fail_after_first):
        with pytest.raises(StorageError):
            await create_current_state_snapshot(
                connection_qualified_name=CONN_QN,
                transformed_dir=transformed,
                previous_state_dir=previous,
                current_state_dir=dirs.current_state,
                s3_prefix=STATE_PREFIX.removesuffix("/current-state"),
                run_id="run-2",
                application_name=APP,
                upload_concurrency=1,
                incremental_diff_dir=dirs.diff,
            )
    _require(landed, "the injected failure must hit mid-upload")

    # The retry's view of the previous state must be one committed snapshot —
    # run 1 whole, since run 2 never committed. Same run, same directory: the
    # retry re-syncs it rather than starting from scratch.
    retry_previous = await materialize_previous_state(_store(), dirs.previous_state)
    assert _tree(retry_previous) == committed, (
        "retry downloaded a previous state mixing run 1 and a failed run 2: "
        f"{sorted(k for k, v in _tree(retry_previous).items() if committed.get(k) != v)}"
        " differ from the committed snapshot"
    )


# ---------------------------------------------------------------------------
# #3 — live writer thread still in the local current-state (FND-3011 shape)
# ---------------------------------------------------------------------------


async def test_retry_is_isolated_from_a_live_writer_in_current_state(
    tmp_path: Path, store: LocalStore, staging: Path
) -> None:
    await _seed_state(tmp_path, {"table/chunk-0.json": [_table(T1)]})

    # The directory every attempt of every run of the connection used to
    # share — where an abandoned attempt's uncancellable offloaded copy (or an
    # older SDK on the same worker) may still be writing.
    shared_dir = helpers.get_persistent_artifacts_path(CONN_QN, "current-state", APP)
    (shared_dir / "column").mkdir(parents=True)

    stop = threading.Event()
    started = threading.Event()

    def _abandoned_attempt_copy() -> None:
        i = 0
        while not stop.is_set():
            dest = shared_dir / "column"
            try:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / f"stale-attempt-{i}.json").write_text("{}", encoding="utf-8")
            except OSError:
                pass
            i += 1
            started.set()

    writer = threading.Thread(target=_abandoned_attempt_copy, daemon=True)
    writer.start()
    try:
        # Without a live writer the retry trivially finds no foreign files,
        # which would read as "isolated" rather than "never raced".
        _require(
            started.wait(timeout=5), "the abandoned-attempt writer must be running"
        )
        # The retry's read only probes; the files land in its own directory.
        read = await _extractor().read_current_state(
            ReadCurrentStateInput(
                connection_qualified_name=CONN_QN,
                application_name=APP,
                output_path=str(tmp_path / "run-2"),
            )
        )
        state_dir = await materialize_previous_state(
            CurrentStateStore(read.current_state_s3_prefix),
            RunStateDirs.for_output_path(tmp_path / "run-2").previous_state,
        )
        # Let the abandoned attempt keep writing past the retry's download.
        await asyncio.sleep(0.05)
    finally:
        stop.set()
        writer.join(timeout=5)

    _require(read.current_state_available, "the retry must find the seeded state")
    assert read.current_state_path == "", "the probe must not download anything"
    foreign = sorted(p.name for p in state_dir.rglob("stale-attempt-*.json"))
    assert not foreign, (
        f"retry's state directory holds {len(foreign)} files written by the "
        "abandoned attempt"
    )
    assert _tree(state_dir) == {"table/chunk-0.json": json.dumps(_table(T1)).encode()}


# ---------------------------------------------------------------------------
# #4 — two concurrent runs of one connection on one worker
# ---------------------------------------------------------------------------


async def test_concurrent_runs_of_one_connection_use_distinct_directories(
    tmp_path: Path, store: LocalStore, staging: Path
) -> None:
    seeded = _tree(await _seed_state(tmp_path, {"table/chunk-0.json": [_table(T1)]}))

    dirs_a = RunStateDirs.for_output_path(tmp_path / "run-a")
    dirs_b = RunStateDirs.for_output_path(tmp_path / "run-b")
    prev_a, prev_b = await asyncio.gather(
        materialize_previous_state(_store(), dirs_a.previous_state),
        materialize_previous_state(_store(), dirs_b.previous_state),
    )

    assert prev_a != prev_b, f"both runs download previous state into {prev_a}"
    assert dirs_a.current_state != dirs_b.current_state
    assert dirs_a.diff != dirs_b.diff
    # Neither run's download disturbed the other's.
    assert _tree(prev_a) == seeded
    assert _tree(prev_b) == seeded


# ---------------------------------------------------------------------------
# #5 — first-run marker read and non-not-found errors
# ---------------------------------------------------------------------------


async def test_first_run_marker_read_is_not_a_warning(
    store: LocalStore, staging: Path, loguru_capture: list[dict]
) -> None:
    marker, _next = await fetch_marker_from_storage(CONN_QN, APP)
    _require(marker is None, "an empty store must yield no marker")

    tracebacks = [
        r["message"]
        for r in loguru_capture
        if r["level"].no >= 30 and r["exception"] is not None
    ]
    assert not tracebacks, f"first run logged a warning traceback: {tracebacks}"


async def test_marker_read_failure_other_than_not_found_propagates(
    store: LocalStore, staging: Path
) -> None:
    outage = StorageError("injected: store unavailable")
    with patch.object(helpers, "_get_bytes", AsyncMock(side_effect=outage)):
        try:
            marker, _next = await fetch_marker_from_storage(CONN_QN, APP)
        except StorageError:
            return
    raise AssertionError(
        f"a non-not-found StorageError was swallowed; marker={marker!r} makes "
        "this run a full extraction"
    )


# ---------------------------------------------------------------------------
# #6 — the template never wires backfill detection into the diff
# ---------------------------------------------------------------------------


async def test_write_current_state_diff_includes_backfill_tables(
    tmp_path: Path, store: LocalStore, staging: Path
) -> None:
    # Previous state knows only T1; T2 is present now but NO CHANGE — the
    # textbook backfill case (it exists, but was never captured).
    await _seed_state(tmp_path, {"table/chunk-0.json": [_table(T1)]})

    output_path = tmp_path / "run-2"
    transformed = output_path / "transformed"
    _write_jsonl(transformed / "table" / "chunk-0.json", [_table(T1), _table(T2)])
    _write_jsonl(transformed / "column" / "chunk-0.json", [_column(T2, "B")])

    from application_sdk.common.incremental.state import state_writer

    # Only the transformed-output download is short-circuited — its prefix
    # derivation depends on a workflow output path, not on anything under test.
    with patch.object(
        state_writer, "download_transformed_data", AsyncMock(return_value=transformed)
    ):
        out = await _extractor("run-2").write_current_state(
            WriteCurrentStateInput(
                workflow_id="wf",
                connection=ConnectionRef(
                    attributes=ConnectionAttributes(qualified_name=CONN_QN, name="c")
                ),
                output_path=str(output_path),
                current_state_available=True,
                current_state_s3_prefix=STATE_PREFIX,
                application_name=APP,
            )
        )

    _require(bool(out.incremental_diff_path), "an incremental diff must be built")
    metadata = json.loads(
        (Path(out.incremental_diff_path) / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["tables_backfill"] == 1, (
        f"diff metadata reports tables_backfill={metadata['tables_backfill']}; "
        f"{T2} is new to state and should be backfilled"
    )


# ---------------------------------------------------------------------------
# #7 — a killed attempt's partial tree is consumed as complete
# ---------------------------------------------------------------------------


async def _prepare_column_queries(tmp_path: Path, output_path: Path) -> tuple[int, int]:
    """Run prepare_column_extraction_queries; return (backfill, changed) counts."""
    transformed_prefix = "artifacts/example/run-2/transformed"
    await upload_prefix(
        local_dir=str(tmp_path / "transformed-src"), prefix=transformed_prefix
    )

    def _prefix(path: str) -> str:
        return (
            transformed_prefix
            if path.endswith("transformed")
            else "artifacts/example/run-2/batches"
        )

    with patch("application_sdk.execution.get_object_store_prefix", _prefix):
        out = await _extractor().prepare_column_extraction_queries(
            PrepareColumnQueriesInput(
                output_path=str(output_path),
                column_batch_size=10,
                connection_qualified_name=CONN_QN,
                application_name=APP,
                current_state_available=True,
                current_state_s3_prefix=STATE_PREFIX,
            )
        )
    return out.backfill_tables, out.changed_tables


@pytest.mark.parametrize("partial_in", ["connection-dir", "run-dir"])
async def test_partial_local_state_from_killed_attempt_is_not_trusted(
    tmp_path: Path, store: LocalStore, staging: Path, partial_in: str
) -> None:
    # The committed snapshot knows both tables, split across two files.
    await _seed_state(
        tmp_path,
        {
            "table/chunk-0.json": [_table(T1)],
            "table/chunk-1.json": [_table(T2)],
        },
    )
    # This run sees both tables unchanged: nothing is new, nothing to backfill.
    _write_jsonl(
        tmp_path / "transformed-src" / "table" / "chunk-0.json",
        [_table(T1), _table(T2)],
    )

    # A previous attempt on this worker was killed after downloading only the
    # first file: into the old fixed per-connection directory, or into this
    # run's own previous-state directory (a same-run retry).
    local_state = (
        helpers.get_persistent_artifacts_path(CONN_QN, "current-state", APP)
        if partial_in == "connection-dir"
        else RunStateDirs.for_output_path(tmp_path / "run-2").previous_state
    )
    _write_jsonl(local_state / "table" / "chunk-0.json", [_table(T1)])

    backfill, changed = await _prepare_column_queries(tmp_path, tmp_path / "run-2")

    _require(changed == 0, "both tables must be NO CHANGE")
    assert backfill == 0, (
        f"{backfill} table(s) flagged for backfill: the partial local tree was "
        "trusted as the previous state instead of the committed snapshot"
    )


# ---------------------------------------------------------------------------
# #8 — tree walks on the event loop
# ---------------------------------------------------------------------------


@pytest.fixture
def loop_thread_walks(
    monkeypatch: pytest.MonkeyPatch, staging: Path
) -> list[tuple[str, Path]]:
    """Record every Path.glob/rglob under *staging* made on the loop thread.

    Scale-independent: a walk that runs on the loop thread holds it for time
    proportional to the tree, which at 100k files is the heartbeat starvation
    the ADR-0010 offloads exist to prevent. Asserting on the thread, not on a
    measured stall, keeps the test deterministic (a wall-clock gap measures
    the GIL as much as the walk).
    """
    loop_thread = threading.get_ident()
    calls: list[tuple[str, Path]] = []
    real_glob, real_rglob = Path.glob, Path.rglob

    def _recording(name, real):
        def _walk(self, *args, **kwargs):
            if threading.get_ident() == loop_thread and self.is_relative_to(staging):
                calls.append((name, self))
            return real(self, *args, **kwargs)

        return _walk

    monkeypatch.setattr(Path, "glob", _recording("glob", real_glob))
    monkeypatch.setattr(Path, "rglob", _recording("rglob", real_rglob))
    return calls


async def test_state_tree_walks_run_off_the_event_loop(
    tmp_path: Path,
    store: LocalStore,
    staging: Path,
    loop_thread_walks: list[tuple[str, Path]],
) -> None:
    await _seed_state(
        tmp_path,
        {f"table/chunk-{i}.json": [_table(f"{SCHEMA}/T{i}")] for i in range(20)},
    )
    _write_jsonl(
        tmp_path / "transformed-src" / "table" / "chunk-0.json",
        [_table(f"{SCHEMA}/T{i}") for i in range(20)],
    )

    await _extractor().read_current_state(
        ReadCurrentStateInput(connection_qualified_name=CONN_QN, application_name=APP)
    )
    await _prepare_column_queries(tmp_path, tmp_path / "run-2")

    assert not loop_thread_walks, (
        "state tree walked on the event loop thread: "
        f"{[(name, str(p.relative_to(staging))) for name, p in loop_thread_walks]}"
    )
