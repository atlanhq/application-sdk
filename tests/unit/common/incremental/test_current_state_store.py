"""Tests for ``CurrentStateStore``: probe, materialize, commit (FND-3064).

Real stores throughout — a ``LocalStore`` where on-disk layout matters, an
obstore ``MemoryStore`` for the 100k-key probe — with only failure injection
mocked.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import obstore
import orjson
import pytest
from obstore.store import LocalStore, MemoryStore

from application_sdk._runtime.offload import run_in_thread
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.common._listing import has_internal_component
from application_sdk.common.incremental import helpers
from application_sdk.common.incremental.incremental_errors import (
    CurrentStateManifestError,
)
from application_sdk.common.incremental.marker import fetch_marker_from_storage
from application_sdk.common.incremental.state import store as store_module
from application_sdk.common.incremental.state.store import (
    MANIFEST_NAME,
    CurrentStateStore,
    DamagedManifestPolicy,
    RunStateDirs,
)
from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef
from application_sdk.infrastructure.context import (
    InfrastructureContext,
    clear_infrastructure,
    set_infrastructure,
)
from application_sdk.storage import batch as batch_module
from application_sdk.storage.batch import list_keys, upload_prefix
from application_sdk.storage.errors import StorageError
from application_sdk.storage.factory import create_local_store
from application_sdk.templates.contracts.incremental_sql import (
    ExecuteColumnBatchInput,
    FetchColumnsIncrementalInput,
    FetchTablesIncrementalInput,
    IncrementalExtractionInput,
    IncrementalRunContext,
)
from application_sdk.templates.contracts.sql_metadata import (
    FetchColumnsOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesOutput,
)
from application_sdk.templates.incremental_sql_metadata_extractor import (
    IncrementalSqlMetadataExtractor,
)

PREFIX = "persistent-artifacts/apps/example-app/connection/1700000000/current-state"


def _files(root: Path) -> dict[str, bytes]:
    """Data files under *root* (SDK working dirs excluded), by relative path."""
    return {
        rel: p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and not has_internal_component(rel := p.relative_to(root).as_posix())
    }


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


def _relative(keys) -> set[str]:
    return {k.removeprefix(PREFIX + "/") for k in keys}


@pytest.fixture
def local(tmp_path: Path) -> Iterator[LocalStore]:
    store = create_local_store(tmp_path / "store")
    set_infrastructure(InfrastructureContext(storage=store))
    try:
        yield store
    finally:
        clear_infrastructure()


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


class TestProbe:
    async def test_absent_state_is_a_first_run(self, local) -> None:
        snapshot = await CurrentStateStore(PREFIX).probe()
        assert snapshot.exists is False
        assert snapshot.json_count == 0
        assert snapshot.committed_run_id is None
        assert snapshot.keys == ()

    async def test_legacy_snapshot_without_manifest_is_trusted_minus_strays(
        self, local, tmp_path
    ) -> None:
        """A pre-manifest snapshot is its listing — except run-stamped keys,
        which only a commit that never reached its manifest writes."""
        seed = _write(
            tmp_path / "seed",
            {
                "table/chunk-0.json": "{}\n",
                "column/chunk-0.json": "{}\n",
                "column/0123456789ab--chunk-0.json": "{}\n",
            },
        )
        await upload_prefix(str(seed), PREFIX)

        snapshot = await CurrentStateStore(PREFIX).probe()

        assert _relative(snapshot.keys) == {"table/chunk-0.json", "column/chunk-0.json"}
        assert snapshot.json_count == 2
        assert snapshot.committed_run_id is None

    async def test_manifest_is_trusted_over_the_listing(self, local, tmp_path) -> None:
        state = CurrentStateStore(PREFIX)
        await state.commit(_write(tmp_path / "b", {"table/chunk-0.json": "{}\n"}), "r1")
        # A later commit's partial upload that never reached its manifest.
        await upload_prefix(
            str(_write(tmp_path / "stray", {"table/aaaaaaaaaaaa--chunk-0.json": "x"})),
            PREFIX,
        )

        snapshot = await state.probe()

        assert snapshot.committed_run_id == "r1"
        assert len(snapshot.keys) == 1
        assert "aaaaaaaaaaaa--" not in snapshot.keys[0]

    async def test_manifest_naming_a_missing_key_fails_loudly(
        self, local, tmp_path
    ) -> None:
        """Diffing against a partial snapshot would report its gaps as deletions."""
        state = CurrentStateStore(PREFIX)
        committed = await state.commit(
            _write(tmp_path / "b", {"table/a.json": "{}\n", "table/b.json": "{}\n"}),
            "r1",
        )
        await obstore.delete_async(local, committed.keys[0])

        with pytest.raises(CurrentStateManifestError, match="does not hold"):
            await state.probe()

    async def test_unreadable_manifest_fails_loudly(self, local) -> None:
        await obstore.put_async(local, f"{PREFIX}/{MANIFEST_NAME}", b"not json")
        with pytest.raises(CurrentStateManifestError, match="unreadable"):
            await CurrentStateStore(PREFIX).probe()

    async def test_damaged_manifest_can_be_treated_as_absent(
        self, local, tmp_path
    ) -> None:
        state = CurrentStateStore(PREFIX)
        committed = await state.commit(
            _write(tmp_path / "b", {"table/a.json": "{}\n", "table/b.json": "{}\n"}),
            "r1",
        )
        await obstore.delete_async(local, committed.keys[0])

        snapshot = await state.probe(
            on_damaged_manifest=DamagedManifestPolicy.TREAT_AS_ABSENT
        )

        assert snapshot.manifest_discarded
        assert not snapshot.exists
        assert snapshot.keys == ()
        assert snapshot.committed_run_id is None

    async def test_unreadable_manifest_can_be_treated_as_absent(self, local) -> None:
        await obstore.put_async(local, f"{PREFIX}/{MANIFEST_NAME}", b"not json")
        snapshot = await CurrentStateStore(PREFIX).probe(
            on_damaged_manifest=DamagedManifestPolicy.TREAT_AS_ABSENT
        )
        assert snapshot.manifest_discarded
        assert not snapshot.exists

    @pytest.mark.parametrize("failing", ["list_data_objects", "_get_bytes"])
    async def test_a_storage_failure_still_raises_when_treating_as_absent(
        self, local, tmp_path, failing
    ) -> None:
        """A retry can fix a failed read, so it must not become a full extraction."""
        state = CurrentStateStore(PREFIX)
        await state.commit(_write(tmp_path / "b", {"table/a.json": "{}\n"}), "r1")

        async def _fail(*args, **kwargs):
            raise StorageError("injected: read")

        with patch.object(store_module, failing, _fail):
            with pytest.raises(StorageError, match="injected"):
                await state.probe(
                    on_damaged_manifest=DamagedManifestPolicy.TREAT_AS_ABSENT
                )

    async def test_manifest_name_is_invisible_to_the_publish_glob(self) -> None:
        """Publish globs ``current-state/**/*.json``; the manifest must not match."""
        assert MANIFEST_NAME.startswith(".")
        assert not MANIFEST_NAME.endswith(".json")

    async def test_probe_of_100k_keys_is_fast_and_never_starves_the_loop(
        self,
    ) -> None:
        memory = MemoryStore()
        names = [f"column/0123456789ab--chunk-{i}.json" for i in range(100_000)]
        for start in range(0, len(names), 2000):
            await asyncio.gather(
                *[
                    obstore.put_async(memory, f"{PREFIX}/{n}", b"{}")
                    for n in names[start : start + 2000]
                ]
            )
        manifest = {"version": 1, "run_id": "r1", "keys": {n: 2 for n in names}}
        await obstore.put_async(
            memory, f"{PREFIX}/{MANIFEST_NAME}", orjson.dumps(manifest)
        )

        # Structural, not a loop-gap timer: at this size the reconcile held
        # inline blocks the loop for ~50ms, well under any threshold a shared CI
        # runner's GIL contention stays below, so a timer could only flake.
        offloaded: list[str] = []
        real_run_in_thread = store_module.run_in_thread

        async def _spy(fn, *args, **kwargs):
            offloaded.append(fn.__name__)
            return await real_run_in_thread(fn, *args, **kwargs)

        started = time.perf_counter()
        with patch.object(store_module, "run_in_thread", _spy):
            snapshot = await CurrentStateStore(PREFIX, memory).probe()
        elapsed = time.perf_counter() - started

        assert snapshot.json_count == 100_000
        assert elapsed < 10, f"probe of 100k keys took {elapsed:.1f}s"
        # The one pass over the whole snapshot (manifest decode + reconcile)
        # runs off the loop, so an activity's auto-heartbeat keeps beating.
        assert offloaded == ["_reconcile"]


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


class TestCommit:
    async def test_commit_replaces_the_snapshot_whole(self, local, tmp_path) -> None:
        state = CurrentStateStore(PREFIX)
        await state.commit(
            _write(
                tmp_path / "r1", {"table/chunk-0.json": "1", "column/chunk-1.json": "1"}
            ),
            "run-1",
        )
        build = _write(tmp_path / "r2", {"table/chunk-0.json": "2"})
        committed = await state.commit(build, "run-2")

        in_store = _relative(await list_keys(PREFIX))
        local_names = set(_files(build))
        # Exactly run 2's keys, their sidecars, and the manifest.
        assert in_store == (
            local_names | {f"{n}.sha256" for n in local_names} | {MANIFEST_NAME}
        )
        # The local build directory mirrors the committed names.
        assert _relative(committed.keys) == local_names
        assert committed.committed_run_id == "run-2"

    async def test_a_commit_that_dies_before_its_manifest_changes_nothing(
        self, local, tmp_path
    ) -> None:
        state = CurrentStateStore(PREFIX)
        first = await state.commit(
            _write(tmp_path / "r1", {"table/chunk-0.json": "1"}), "run-1"
        )

        with patch.object(
            store_module, "_put", side_effect=StorageError("injected: manifest write")
        ):
            with pytest.raises(StorageError):
                await state.commit(
                    _write(tmp_path / "r2", {"table/chunk-0.json": "2"}), "run-2"
                )

        after = await state.probe()
        assert after.committed_run_id == "run-1"
        assert after.keys == first.keys
        dest = await state.materialize(after, tmp_path / "prev")
        assert list(_files(dest).values()) == [b"1"]

    async def test_same_run_retry_overwrites_only_its_own_keys(
        self, local, tmp_path
    ) -> None:
        state = CurrentStateStore(PREFIX)
        a = await state.commit(_write(tmp_path / "a", {"table/c.json": "x"}), "run-1")
        b = await state.commit(_write(tmp_path / "b", {"table/c.json": "x"}), "run-1")
        assert a.keys == b.keys

    async def test_carried_forward_files_keep_distinct_names(
        self, local, tmp_path
    ) -> None:
        """A file materialized from a previous commit, carried into the next
        build beside a fresh file of the same base name, is neither dropped nor
        restamped into an ever-growing name."""
        state = CurrentStateStore(PREFIX)
        first = await state.commit(
            _write(tmp_path / "r1", {"column/chunk-0.json": "old"}), "run-1"
        )
        prev = await state.materialize(first, tmp_path / "prev")
        carried = next(iter(_files(prev)))

        build = _write(tmp_path / "r2", {"column/chunk-0.json": "new"})
        _write(build, {carried: "old"})
        second = await state.commit(build, "run-2")

        names = sorted(_relative(second.keys))
        assert len(names) == 2
        assert sorted(_files(build).values()) == [b"new", b"old"]
        assert all(n.count("--") <= 2 for n in names)

    async def test_prune_defers_to_a_later_commit(self, local, tmp_path) -> None:
        """If another run committed after us, its keys are the live snapshot."""
        state = CurrentStateStore(PREFIX)
        first = await state.commit(_write(tmp_path / "a", {"t/a.json": "a"}), "run-a")
        await state.commit(_write(tmp_path / "b", {"t/b.json": "b"}), "run-b")

        # run-a's prune, arriving late: the manifest now names run-b.
        await state._prune(set(first.keys), "run-a", tmp_path / "a")

        assert (await state.probe()).committed_run_id == "run-b"

    async def test_a_failed_runs_uploads_are_pruned_by_the_next_commit(
        self, local, tmp_path
    ) -> None:
        """A run that uploads and dies before its manifest leaves stamped keys
        the publish glob would read. The next commit prunes them at once."""
        state = CurrentStateStore(PREFIX)
        with patch.object(
            store_module, "_put", side_effect=StorageError("injected: dies")
        ):
            with pytest.raises(StorageError):
                await state.commit(
                    _write(tmp_path / "d", {"t/d.json": "d"}), "run-dead"
                )
        assert any("d.json" in k for k in await list_keys(PREFIX))

        committed = await state.commit(
            _write(tmp_path / "a", {"t/a.json": "a"}), "run-a"
        )

        in_store = set(await list_keys(PREFIX))
        assert in_store == (
            set(committed.keys)
            | {f"{k}.sha256" for k in committed.keys}
            | {state.manifest_key}
        )

    async def test_overlapping_attempts_of_one_run_keep_each_others_keys(
        self, local, tmp_path
    ) -> None:
        """A timed-out attempt still running beside its retry: the attempt that
        committed first prunes late, after the retry's manifest is live. It must
        not delete the retry's keys, which carry the same stamp.

        The two attempts get *different* trees here on purpose, as a stress
        case. Real attempts of one run build from the same ``transformed/``
        prefix and produce identical names, so each overwrites the other and
        there is nothing to prune. With different trees, the earlier attempt's
        file survives until the next run's commit (asserted below): a stale
        file for one run, rather than a live manifest naming deleted keys."""
        state = CurrentStateStore(PREFIX)
        first = await state.commit(_write(tmp_path / "a1", {"t/a.json": "a"}), "run-x")
        await state.commit(_write(tmp_path / "a2", {"t/b.json": "b"}), "run-x")

        # Attempt 1's prune, arriving after attempt 2's commit and prune.
        await state._prune(set(first.keys), "run-x", tmp_path / "a1")

        snapshot = await state.probe()  # would raise CurrentStateManifestError
        dest = await state.materialize(snapshot, tmp_path / "out")
        assert sorted(_files(dest).values()) == [b"b"]

        # Attempt 1's leftover goes with the next run's commit.
        committed = await state.commit(
            _write(tmp_path / "next", {"t/c.json": "c"}), "run-next"
        )
        assert set(await list_keys(PREFIX)) == (
            set(committed.keys)
            | {f"{k}.sha256" for k in committed.keys}
            | {state.manifest_key}
        )

    async def test_a_committed_key_deleted_before_the_prune_is_re_uploaded(
        self, local, tmp_path
    ) -> None:
        """If an overlapping commit (which scheduling rules out) deleted one of
        this run's keys, the run whose manifest is live restores it, so later
        probes are not refused a manifest naming deleted keys."""
        state = CurrentStateStore(PREFIX)
        victim = f"{state._key_prefix}t/{store_module._run_stamp('run-b')}b.json"
        real_put = store_module._put

        async def put_then_lose_a_key(key, *args, **kwargs):
            await real_put(key, *args, **kwargs)
            if key == state.manifest_key:
                await obstore.delete_async(store_module._resolve_store(None), victim)

        with patch.object(store_module, "_put", side_effect=put_then_lose_a_key):
            await state.commit(
                _write(tmp_path / "b", {"t/b.json": "b", "t/c.json": "c"}), "run-b"
            )

        snapshot = await state.probe()  # would raise CurrentStateManifestError
        assert victim in snapshot.keys
        dest = await state.materialize(snapshot, tmp_path / "out")
        assert sorted(_files(dest).values()) == [b"b", b"c"]

    async def test_committing_a_missing_directory_keeps_the_snapshot(
        self, local, tmp_path
    ) -> None:
        state = CurrentStateStore(PREFIX)
        first = await state.commit(_write(tmp_path / "r1", {"t/a.json": "a"}), "r1")

        with pytest.raises(FileNotFoundError):
            await state.commit(tmp_path / "never-built", "r2")

        after = await state.probe()
        assert after.committed_run_id == "r1"
        assert after.keys == first.keys


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


class TestMaterialize:
    async def test_mirrors_exactly_the_snapshot(self, local, tmp_path) -> None:
        state = CurrentStateStore(PREFIX)
        await state.commit(
            _write(tmp_path / "b", {"table/a.json": "a", "table/b.json": "b"}), "r1"
        )
        snapshot = await state.probe()
        dest = tmp_path / "incremental" / "previous-state"
        # A killed attempt's partial tree, plus a file no snapshot names.
        _write(dest, {"table/foreign.json": "?"})

        await state.materialize(snapshot, dest)

        assert sorted(_files(dest).values()) == [b"a", b"b"]

    async def test_a_hand_built_snapshot_is_relisted(self, local, tmp_path) -> None:
        """A snapshot from ``commit`` carries no listing; materialize lists."""
        state = CurrentStateStore(PREFIX)
        committed = await state.commit(_write(tmp_path / "b", {"t/a.json": "a"}), "r1")
        dest = await state.materialize(committed, tmp_path / "prev")
        assert list(_files(dest).values()) == [b"a"]

    async def test_an_object_vanishing_after_the_probe_fails_the_read(
        self, local, tmp_path
    ) -> None:
        """A key the probe listed but that is gone by its download must fail
        the materialize, so the task retries rather than diffing a partial
        previous state."""
        state = CurrentStateStore(PREFIX)
        await state.commit(
            _write(tmp_path / "b", {"t/a.json": "a", "t/b.json": "b"}), "r1"
        )
        snapshot = await state.probe()
        await obstore.delete_async(local, snapshot.keys[0])

        with pytest.raises(StorageError):
            await state.materialize(snapshot, tmp_path / "prev")

    async def test_a_retry_after_a_cancelled_materialize_is_not_overwritten(
        self, local, tmp_path
    ) -> None:
        """A timed-out read's attempt is cancelled, but its offloaded download
        thread is not: it keeps writing into the tree. The cancellation must
        not surface until that thread is done, or its late write lands on top
        of the retry's freshly materialized file (FND-3011)."""
        state = CurrentStateStore(PREFIX)
        await state.commit(
            _write(tmp_path / "b", {"t/a.json": "a", "t/b.json": "b"}), "r1"
        )
        snapshot = await state.probe()
        dest = tmp_path / "incremental" / "previous-state"
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def _abandoned_write(path: str) -> None:
            started.set()
            # Held until the cancel is requested, so the attempt is still
            # in flight when it is cancelled however late the loop runs.
            release.wait(5)
            # Outlasts the retry's materialize, so a missing drain lands this
            # write after the retry rather than before it.
            time.sleep(0.3)
            Path(path).write_bytes(b"written by the cancelled attempt")
            finished.set()

        real_download = batch_module.download_file_chunked

        async def _download(key, dest_path, *args, **kwargs):
            if key.endswith("b.json"):
                await run_in_thread(_abandoned_write, dest_path)
            else:
                await real_download(key, dest_path, *args, **kwargs)

        with patch.object(batch_module, "download_file_chunked", _download):
            attempt = asyncio.create_task(state.materialize(snapshot, dest))
            assert await asyncio.to_thread(started.wait, 5), "download never began"
            attempt.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await attempt
        assert finished.is_set(), "cancelled attempt's thread outlived its unwind"

        await state.materialize(snapshot, dest)
        # Without the drain the thread is still asleep here; give it the time
        # to land its write so a regression shows in the tree, not as a race.
        await asyncio.to_thread(finished.wait, 5)

        assert sorted(_files(dest).values()) == [b"a", b"b"]


def test_run_state_dirs_are_scoped_to_the_output_path(tmp_path) -> None:
    a = RunStateDirs.for_output_path(tmp_path / "run-a")
    b = RunStateDirs.for_output_path(tmp_path / "run-b")
    assert a.previous_state == tmp_path / "run-a" / "incremental" / "previous-state"
    assert a.current_state == tmp_path / "run-a" / "incremental" / "current-state"
    assert a.diff == tmp_path / "run-a" / "incremental" / "diff"
    assert {a.previous_state, a.current_state, a.diff}.isdisjoint(
        {b.previous_state, b.current_state, b.diff}
    )


# ---------------------------------------------------------------------------
# Two runs end to end through the template's run()
# ---------------------------------------------------------------------------

CONN_QN = "default/example/1700000000"
APP = "example-app"
SCHEMA = f"{CONN_QN}/DB/SCHEMA"


def _table(name: str, state: str) -> dict:
    return {
        "typeName": "Table",
        "status": "ACTIVE",
        "attributes": {
            "qualifiedName": f"{SCHEMA}/{name}",
            "name": name,
            "databaseName": "DB",
            "schemaName": "SCHEMA",
        },
        "customAttributes": json.dumps({"incremental_state": state}),
    }


def _column(table: str, name: str) -> dict:
    return {
        "typeName": "Column",
        "status": "ACTIVE",
        "attributes": {
            "qualifiedName": f"{SCHEMA}/{table}/{name}",
            "name": name,
            "tableQualifiedName": f"{SCHEMA}/{table}",
        },
        "customAttributes": "{}",
    }


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)

    class _ConnectorBase(IncrementalSqlMetadataExtractor):
        """A connector whose "source" is ``self.tables``: name -> state.

        Still abstract (``build_incremental_column_sql`` is left to the
        subclass the ``env`` fixture defines), so importing this module
        registers no app and no task: the concrete class is created inside
        the registry-reset fixtures and gone with them.
        """

        tables: dict[str, str]

        def _key(self, path: Path) -> str:
            return "artifacts/e2e/" + path.relative_to(self.root).as_posix()

        async def _publish(self, output_path: str, rel: str, rows: list[dict]) -> None:
            path = Path(output_path) / "transformed" / rel
            _jsonl(path, rows)
            await upload_prefix(
                str(Path(output_path) / "transformed"),
                self._key(Path(output_path) / "transformed"),
            )

        @task(timeout_seconds=60)
        async def fetch_databases(
            self, input: FetchDatabasesInput
        ) -> FetchDatabasesOutput:
            return FetchDatabasesOutput()

        @task(timeout_seconds=60)
        async def fetch_schemas(self, input: FetchSchemasInput) -> FetchSchemasOutput:
            return FetchSchemasOutput()

        @task(timeout_seconds=60)
        async def fetch_tables(
            self, input: FetchTablesIncrementalInput
        ) -> FetchTablesOutput:
            rows = [_table(n, s) for n, s in self.tables.items()]
            await self._publish(input.output_path, "table/chunk-0.json", rows)
            return FetchTablesOutput(total_record_count=len(rows))

        @task(timeout_seconds=60)
        async def fetch_columns(
            self, input: FetchColumnsIncrementalInput
        ) -> FetchColumnsOutput:
            if input.marker_timestamp and input.current_state_available:
                return FetchColumnsOutput()
            rows = [_column(n, "ID") for n in self.tables]
            await self._publish(input.output_path, "column/chunk-0.json", rows)
            return FetchColumnsOutput(total_record_count=len(rows))

        async def execute_column_sql(
            self, sql: str, input: ExecuteColumnBatchInput, ctx: IncrementalRunContext
        ) -> int:
            rows = [_column(t.rsplit("/", 1)[-1], "ID") for t in sql.split(",")]
            await self._publish(
                input.output_path, f"column/batch-{input.batch_index}.json", rows
            )
            return len(rows)


class TestTwoRunsEndToEnd:
    """Run 1 full; run 2 incremental with one table dropped and one added."""

    @pytest.fixture
    def env(
        self, tmp_path, local, monkeypatch, clean_app_registry, clean_task_registry
    ):
        monkeypatch.setattr(helpers, "TEMPORARY_PATH", str(tmp_path / "staging"))
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", APP)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            class _Connector(_ConnectorBase):
                def build_incremental_column_sql(
                    self, table_ids: list[str], ctx: IncrementalRunContext
                ) -> str:
                    return ",".join(table_ids)

        connector = _Connector.__new__(_Connector)
        connector.root = tmp_path

        def _prefix(path: str) -> str:
            return connector._key(Path(path))

        with (
            patch("application_sdk.execution.get_object_store_prefix", _prefix),
            patch(
                "application_sdk.common.incremental.state.state_writer."
                "get_object_store_prefix",
                _prefix,
            ),
        ):
            yield connector

    async def _run(self, connector: _ConnectorBase, tmp_path: Path, run: str):
        # In production the worker gives every task this run's Temporal run ID
        # through the app context; the tasks here are plain calls.
        connector._context = AppContext(
            app_name=APP, app_version="0.1.0", run_id=run, workflow_id="wf"
        )
        with patch(
            "temporalio.workflow.info", return_value=SimpleNamespace(run_id=run)
        ):
            return await IncrementalSqlMetadataExtractor.run(
                connector,
                IncrementalExtractionInput(
                    workflow_id="wf",
                    connection=ConnectionRef(
                        attributes=ConnectionAttributes(
                            qualified_name=CONN_QN, name="c"
                        )
                    ),
                    output_path=str(tmp_path / run),
                    incremental_extraction=True,
                    prepone_marker_timestamp=False,
                ),
            )

    async def _marker(self) -> str | None:
        return await helpers.download_marker_from_s3(CONN_QN, APP)

    async def test_second_run_commits_its_own_snapshot_and_diffs_the_first(
        self, env, tmp_path
    ) -> None:
        env.tables = {"KEEP": "CREATED", "DROP": "CREATED"}
        first = await self._run(env, tmp_path, "run-1")
        assert first.marker_updated
        marker_after_run_1 = await self._marker()

        # Run 2: DROP is gone at source; ADD newly enters the filter unchanged,
        # so it is a backfill (present now, never captured before).
        env.tables = {"KEEP": "NO CHANGE", "ADD": "NO CHANGE"}
        marker, _ = await fetch_marker_from_storage(CONN_QN, APP)
        assert marker == marker_after_run_1
        await asyncio.sleep(1.1)  # the marker has one-second resolution
        second = await self._run(env, tmp_path, "run-2")

        # current-state holds exactly run 2's keys, plus the manifest.
        local_build = tmp_path / "run-2" / "incremental" / "current-state"
        run_2_files = set(_files(local_build))
        in_store = {
            k for k in _relative(await list_keys(PREFIX)) if not k.endswith(".sha256")
        }
        assert in_store == run_2_files | {MANIFEST_NAME}
        stamp = store_module._run_stamp("run-2")
        assert all(Path(k).name.startswith(stamp) for k in run_2_files)
        snapshot = await CurrentStateStore(PREFIX).probe()
        assert snapshot.committed_run_id == "run-2"

        # The diff carries the deletion and the backfill.
        diff = tmp_path / "run-2" / "incremental" / "diff"
        metadata = json.loads((diff / "metadata.json").read_text(encoding="utf-8"))
        assert metadata["tables_deleted"] == 1
        assert metadata["tables_backfill"] == 1
        deleted = [
            json.loads(line)["attributes"]["qualifiedName"]
            for f in (diff / "delete" / "table").glob("*.json")
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert deleted == [f"{SCHEMA}/DROP"]
        assert second.backfill_tables == 1

        # Nothing was written to the per-connection local directories.
        assert not (tmp_path / "staging").exists() or not any(
            (tmp_path / "staging").rglob("*.json")
        )
        # And the marker moved only now, after the commit.
        assert second.marker_updated
        assert await self._marker() != marker_after_run_1

    @pytest.mark.parametrize("damage", ["unreadable", "missing_key"])
    async def test_a_damaged_manifest_falls_back_to_a_full_extraction(
        self, env, tmp_path, local, damage
    ) -> None:
        """The run completes in full, and its commit leaves a valid snapshot."""
        env.tables = {"KEEP": "CREATED", "DROP": "CREATED"}
        await self._run(env, tmp_path, "run-1")
        committed = await CurrentStateStore(PREFIX).probe()
        if damage == "unreadable":
            await obstore.put_async(local, f"{PREFIX}/{MANIFEST_NAME}", b"not json")
        else:
            await obstore.delete_async(local, committed.keys[0])
        with pytest.raises(CurrentStateManifestError):
            await CurrentStateStore(PREFIX).probe()

        env.tables = {"KEEP": "NO CHANGE"}
        await asyncio.sleep(1.1)
        second = await self._run(env, tmp_path, "run-2")

        # A full extraction: no previous state was read, so there is no diff
        # and DROP's absence is not reported as a deletion.
        run_dir = tmp_path / "run-2" / "incremental"
        assert not (run_dir / "previous-state").exists()
        assert not (run_dir / "diff").exists()
        assert second.incremental_diff_files == 0
        assert second.marker_updated

        # The commit wrote a fresh manifest and pruned everything it does not name.
        snapshot = await CurrentStateStore(PREFIX).probe()
        assert snapshot.committed_run_id == "run-2"
        assert snapshot.exists
        local_build = tmp_path / "run-2" / "incremental" / "current-state"
        in_store = {
            k for k in _relative(await list_keys(PREFIX)) if not k.endswith(".sha256")
        }
        assert in_store == set(_files(local_build)) | {MANIFEST_NAME}

    async def test_marker_does_not_advance_when_the_commit_fails(
        self, env, tmp_path
    ) -> None:
        env.tables = {"KEEP": "CREATED"}
        await self._run(env, tmp_path, "run-1")
        marker_after_run_1 = await self._marker()
        committed = await CurrentStateStore(PREFIX).probe()

        env.tables = {"KEEP": "NO CHANGE", "ADD": "NO CHANGE"}
        await asyncio.sleep(1.1)
        real_put = store_module._put

        async def _fail_manifest(key, *args, **kwargs):
            if key.endswith(MANIFEST_NAME):
                raise StorageError("injected: manifest write")
            return await real_put(key, *args, **kwargs)

        with patch.object(store_module, "_put", _fail_manifest):
            with pytest.raises(Exception, match="Failed to write current-state"):
                await self._run(env, tmp_path, "run-2")

        assert await self._marker() == marker_after_run_1
        after = await CurrentStateStore(PREFIX).probe()
        assert after.committed_run_id == "run-1"
        assert after.keys == committed.keys
