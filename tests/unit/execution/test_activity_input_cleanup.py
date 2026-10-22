"""Task inputs the wrapper downloaded are deleted when the task ends (FND-4310).

``create_activity_from_task`` materialises durable ``FileReference`` inputs
onto the pod before the task runs. These tests drive a real activity against an
in-memory store and check what is left on local disk afterwards: the copies the
wrapper downloaded are gone once their last task on this process ends, and
nothing else is touched.

The App classes are defined inside each test because
``tests/unit/execution/conftest.py`` resets the registries around every test.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import patch

import pytest

from application_sdk.app.base import App, _app_state, _app_state_lock
from application_sdk.app.registry import TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.storage import DeclaredFile, UploadRefsInput
from application_sdk.contracts.types import FileReference, Lazy
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
)
from application_sdk.infrastructure import context as infra_context
from application_sdk.infrastructure.context import InfrastructureContext
from application_sdk.storage import file_ref_sync
from application_sdk.storage.batch import list_keys
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.reference import fetch, persist_file_reference

pytestmark = pytest.mark.asyncio

_PAYLOAD = b"fnd-4310 " * 128


class _RefIn(Input, allow_unbounded_fields=True):
    ref: FileReference | None = None
    other: FileReference | None = None
    lazy_ref: Annotated[FileReference | None, Lazy()] = None
    gate: str = ""
    fail: bool = False


class _RunIn(Input, allow_unbounded_fields=True):
    name: str = ""


class _SeenOut(Output, allow_unbounded_fields=True):
    seen: str = ""
    path: str = ""


_gates: dict[str, asyncio.Event] = {}
_entered: dict[str, asyncio.Event] = {}
_failures: list[str] = []


def _read(path: str) -> bytes:
    p = Path(path)
    if p.is_dir():
        return b"".join(
            f.read_bytes()
            for f in sorted(p.rglob("*"))
            if f.is_file() and f.suffix != ".sha256"
        )
    return p.read_bytes()


def _consume_activity() -> Any:
    class InputCleanupApp(App):
        @task(timeout_seconds=60)
        async def consume(self, input: _RefIn) -> _SeenOut:
            if input.gate:
                _entered[input.gate].set()
                await _gates[input.gate].wait()
            assert input.ref is not None and input.ref.local_path is not None
            data = _read(input.ref.local_path)
            if input.fail and not _failures:
                _failures.append(input.ref.local_path)
                raise RuntimeError("first attempt fails")
            return _SeenOut(seen=data.decode(), path=input.ref.local_path)

        @task(timeout_seconds=60)
        async def fetch_lazy(self, input: _RefIn) -> _SeenOut:
            assert input.lazy_ref is not None
            local = await fetch(input.lazy_ref)
            return _SeenOut(seen=_read(local.local_path or "").decode())

        @task(timeout_seconds=60)
        async def own_download(self, input: _RefIn) -> _SeenOut:
            assert input.ref is not None and input.ref.local_path is not None
            Path(input.ref.local_path).parent.mkdir(parents=True, exist_ok=True)
            Path(input.ref.local_path).write_bytes(_PAYLOAD)
            return _SeenOut(seen=_PAYLOAD.decode())

        async def run(self, input: _RunIn) -> _SeenOut:
            return _SeenOut()

    return InputCleanupApp


def _activity(task_name: str) -> Any:
    tasks = TaskRegistry.get_instance().get_tasks_for_app("input-cleanup-app")
    return create_activity_from_task(next(t for t in tasks if t.name == task_name))


def _context(task_name: str, workflow_id: str = "wf-fnd4310") -> TaskContext:
    return TaskContext(
        app_name="input-cleanup-app",
        task_name=task_name,
        run_id="run-fnd4310",
        workflow_id=workflow_id,
        heartbeat_timeout_seconds=None,
        auto_heartbeat_seconds=None,
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    memory = create_memory_store()
    monkeypatch.setattr(
        infra_context, "_infrastructure", InfrastructureContext(storage=memory)
    )
    _gates.clear()
    _entered.clear()
    _failures.clear()
    with _app_state_lock:
        _app_state.clear()
    yield memory
    leaked = dict(file_ref_sync._input_leases)
    leaked_state = (
        dict(file_ref_sync._leased_ancestors),
        set(file_ref_sync._deferred_deletes),
    )
    file_ref_sync._input_leases.clear()
    file_ref_sync._leased_ancestors.clear()
    file_ref_sync._deferred_deletes.clear()
    assert not leaked
    assert leaked_state == ({}, set())
    with _app_state_lock:
        _app_state.clear()


def _sidecar(path: str) -> Path:
    return Path(path + ".sha256")


async def _remote_file_ref(store: Any, tmp_path: Path) -> FileReference:
    """A durable ref whose producer copy lives on another pod (absent here)."""
    producer = tmp_path / "producer" / "out.json"
    producer.parent.mkdir(parents=True)
    producer.write_bytes(_PAYLOAD)
    ref = await persist_file_reference(store, FileReference.from_local(producer))
    shutil.rmtree(producer.parent)
    return ref


async def _remote_dir_ref(store: Any, tmp_path: Path) -> FileReference:
    producer = tmp_path / "producer" / "tree"
    (producer / "a").mkdir(parents=True)
    (producer / "a" / "one.json").write_bytes(_PAYLOAD)
    (producer / "two.json").write_bytes(_PAYLOAD)
    ref = await persist_file_reference(store, FileReference.from_local(producer))
    shutil.rmtree(producer.parent)
    return ref


class TestDownloadedInputsAreDeleted:
    async def test_single_file_input_is_deleted_after_the_task(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.seen == _PAYLOAD.decode()
        assert not Path(ref.local_path).exists()
        assert not _sidecar(ref.local_path).exists()

    async def test_directory_input_is_deleted_after_the_task(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_dir_ref(store, tmp_path)
        assert ref.local_path is not None
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.seen == (_PAYLOAD * 2).decode()
        assert not Path(ref.local_path).exists()

    async def test_input_without_local_path_is_deleted_after_the_task(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        ref = ref.model_copy(update={"local_path": None})
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.seen == _PAYLOAD.decode()
        assert out.path
        assert not Path(out.path).exists()
        assert not _sidecar(out.path).exists()

    async def test_failed_attempt_deletes_and_retry_downloads_again(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None
        _consume_activity()
        activity_fn = _activity("consume")

        with pytest.raises(Exception, match="first attempt fails"):
            await activity_fn(_context("consume"), _RefIn(ref=ref, fail=True))
        assert _failures == [ref.local_path]
        assert not Path(ref.local_path).exists()

        out = await activity_fn(_context("consume"), _RefIn(ref=ref, fail=True))

        assert out.seen == _PAYLOAD.decode()
        assert not Path(ref.local_path).exists()


class TestSharedInputOnOnePod:
    async def test_shared_input_survives_until_its_last_task_ends(
        self, store: Any, tmp_path: Path
    ) -> None:
        """Both tasks materialise the same path at once (CONNECT-1126)."""
        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None
        _consume_activity()
        activity_fn = _activity("consume")
        for gate in ("first", "second"):
            _gates[gate] = asyncio.Event()
            _entered[gate] = asyncio.Event()

        first = asyncio.create_task(
            activity_fn(_context("consume"), _RefIn(ref=ref, gate="first"))
        )
        second = asyncio.create_task(
            activity_fn(_context("consume"), _RefIn(ref=ref, gate="second"))
        )
        await _entered["first"].wait()
        await _entered["second"].wait()

        _gates["first"].set()
        assert (await first).seen == _PAYLOAD.decode()
        assert Path(ref.local_path).read_bytes() == _PAYLOAD

        _gates["second"].set()
        assert (await second).seen == _PAYLOAD.decode()
        assert not Path(ref.local_path).exists()

    async def test_task_reusing_a_downloaded_copy_keeps_it_alive(
        self, store: Any, tmp_path: Path
    ) -> None:
        """The second task finds the file current and never materialises it."""
        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None
        _consume_activity()
        activity_fn = _activity("consume")
        _gates["first"] = asyncio.Event()
        _entered["first"] = asyncio.Event()
        _gates["second"] = asyncio.Event()
        _entered["second"] = asyncio.Event()

        first = asyncio.create_task(
            activity_fn(_context("consume"), _RefIn(ref=ref, gate="first"))
        )
        await _entered["first"].wait()
        assert Path(ref.local_path).exists()
        second = asyncio.create_task(
            activity_fn(_context("consume"), _RefIn(ref=ref, gate="second"))
        )
        await _entered["second"].wait()
        _gates["first"].set()
        await first

        assert Path(ref.local_path).read_bytes() == _PAYLOAD
        _gates["second"].set()
        await second
        assert not Path(ref.local_path).exists()


class TestFilesTheWrapperDidNotDownloadAreKept:
    async def test_existing_local_copy_is_not_deleted(
        self, store: Any, tmp_path: Path
    ) -> None:
        """Same-pod hand-off: the producer's copy is already at the path."""
        producer = tmp_path / "producer.json"
        producer.write_bytes(_PAYLOAD)
        ref = await persist_file_reference(store, FileReference.from_local(producer))
        _sidecar(str(producer)).unlink()
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.seen == _PAYLOAD.decode()
        assert producer.read_bytes() == _PAYLOAD

    async def test_auto_materialize_false_ref_is_untouched(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None
        ref = ref.model_copy(update={"auto_materialize": False})
        _consume_activity()

        await _activity("own_download")(_context("own_download"), _RefIn(ref=ref))

        assert Path(ref.local_path).read_bytes() == _PAYLOAD

    async def test_lazy_ref_the_task_fetched_itself_is_untouched(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None
        _consume_activity()

        out = await _activity("fetch_lazy")(
            _context("fetch_lazy"), _RefIn(lazy_ref=ref)
        )

        assert out.seen == _PAYLOAD.decode()
        assert Path(ref.local_path).read_bytes() == _PAYLOAD


class TestFrameworkTasksStillWork:
    async def test_upload_refs_delivers_then_drops_the_downloaded_input(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None and ref.storage_path is not None
        _consume_activity()

        out = await _activity("upload_refs")(
            _context("upload_refs"),
            UploadRefsInput(
                files=[DeclaredFile(ref=ref, label="out.json")],
                prefix="delivered/fnd4310",
            ),
        )

        assert out.prefix == "delivered/fnd4310"
        keys = await list_keys("delivered/fnd4310", store)
        assert any(k.endswith("out.json") for k in keys)
        assert not Path(ref.local_path).exists()

    async def test_cleanup_files_runs_after_inputs_were_deleted(
        self, store: Any, tmp_path: Path
    ) -> None:
        from application_sdk.contracts.cleanup import CleanupInput

        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None
        _consume_activity()

        await _activity("consume")(_context("consume"), _RefIn(ref=ref))
        assert not Path(ref.local_path).exists()

        leftover = tmp_path / "app-scratch.json"
        leftover.write_bytes(_PAYLOAD)
        with patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-fnd4310",
        ):
            out = await _activity("cleanup_files")(
                _context("cleanup_files"), CleanupInput(extra_paths=[str(leftover)])
            )

        assert out.path_results[ref.local_path] is True
        assert out.path_results[str(leftover)] is True
        assert not leftover.exists()


async def _remote_tree_and_member(
    store: Any, tmp_path: Path
) -> tuple[FileReference, FileReference]:
    """A directory ref and a file ref whose local path sits inside it."""
    producer = tmp_path / "producer" / "tree"
    producer.mkdir(parents=True)
    (producer / "one.json").write_bytes(_PAYLOAD)
    (producer / "two.json").write_bytes(_PAYLOAD)
    tree = await persist_file_reference(store, FileReference.from_local(producer))
    member = await persist_file_reference(
        store, FileReference.from_local(producer / "two.json")
    )
    shutil.rmtree(producer.parent)
    return tree, member


class TestFailedMaterialiseLeavesNothing:
    async def test_other_downloads_stop_before_cleanup_runs(
        self, store: Any, tmp_path: Path
    ) -> None:
        failing = await _remote_file_ref(store, tmp_path)
        slow = await _remote_dir_ref(store, tmp_path)
        assert slow.local_path is not None
        release = asyncio.Event()
        _consume_activity()

        async def fake_materialize(
            _store: Any, ref: FileReference, **_: Any
        ) -> FileReference:
            if ref.storage_path == failing.storage_path:
                await asyncio.sleep(0)
                raise RuntimeError("download failed")
            await release.wait()
            Path(ref.local_path or "").mkdir(parents=True, exist_ok=True)
            (Path(ref.local_path or "") / "late.json").write_bytes(_PAYLOAD)
            return ref

        with patch(
            "application_sdk.storage.reference.materialize_file_reference",
            fake_materialize,
        ):
            with pytest.raises(Exception, match="download failed"):
                await _activity("consume")(
                    _context("consume"), _RefIn(ref=failing, other=slow)
                )
            release.set()
            for _ in range(5):
                await asyncio.sleep(0)

        assert not Path(slow.local_path).exists()

    async def test_download_thread_finishes_before_cleanup_runs(
        self, store: Any, tmp_path: Path
    ) -> None:
        import time

        from application_sdk._runtime.offload import run_in_thread

        failing = await _remote_file_ref(store, tmp_path)
        slow = await _remote_dir_ref(store, tmp_path)
        assert slow.local_path is not None
        _consume_activity()

        def write_late(target: str) -> None:
            time.sleep(0.2)
            Path(target).mkdir(parents=True, exist_ok=True)
            (Path(target) / "late.json").write_bytes(_PAYLOAD)

        async def fake_materialize(
            _store: Any, ref: FileReference, **_: Any
        ) -> FileReference:
            if ref.storage_path == failing.storage_path:
                await asyncio.sleep(0.05)
                raise RuntimeError("download failed")
            await run_in_thread(write_late, ref.local_path or "")
            return ref

        with patch(
            "application_sdk.storage.reference.materialize_file_reference",
            fake_materialize,
        ):
            with pytest.raises(Exception, match="download failed"):
                await _activity("consume")(
                    _context("consume"), _RefIn(ref=failing, other=slow)
                )
        await asyncio.sleep(0.4)

        assert not Path(slow.local_path).exists()

    async def test_temp_copy_made_before_a_failure_is_deleted(
        self, store: Any, tmp_path: Path
    ) -> None:
        done = await _remote_file_ref(store, tmp_path)
        done = done.model_copy(update={"local_path": None})
        failing = await _remote_dir_ref(store, tmp_path)
        made: list[str] = []
        _consume_activity()
        from application_sdk.storage import reference

        real = reference.materialize_file_reference

        async def fake_materialize(
            _store: Any, ref: FileReference, **kwargs: Any
        ) -> FileReference:
            if ref.storage_path == failing.storage_path:
                while not made:
                    await asyncio.sleep(0)
                raise RuntimeError("download failed")
            out = await real(_store, ref, **kwargs)
            made.append(out.local_path or "")
            return out

        with patch(
            "application_sdk.storage.reference.materialize_file_reference",
            fake_materialize,
        ):
            with pytest.raises(Exception, match="download failed"):
                await _activity("consume")(
                    _context("consume"), _RefIn(ref=done, other=failing)
                )

        assert made and made[0]
        assert not Path(made[0]).exists()
        assert not Path(made[0]).parent.parent.exists()

    async def test_stopped_download_without_local_path_leaves_nothing(
        self, store: Any, tmp_path: Path
    ) -> None:
        failing = await _remote_file_ref(store, tmp_path)
        stopped = await _remote_dir_ref(store, tmp_path)
        stopped = stopped.model_copy(update={"local_path": None})
        dirs: list[str] = []
        _consume_activity()

        async def fake_materialize(
            _store: Any, ref: FileReference, **kwargs: Any
        ) -> FileReference:
            if ref.storage_path == failing.storage_path:
                while not dirs:
                    await asyncio.sleep(0)
                raise RuntimeError("download failed")
            target = Path(kwargs["local_dir"])
            target.mkdir(parents=True)
            (target / "half.json").write_bytes(_PAYLOAD)
            dirs.append(str(target))
            await asyncio.Event().wait()
            return ref

        with patch(
            "application_sdk.storage.reference.materialize_file_reference",
            fake_materialize,
        ):
            with pytest.raises(Exception, match="download failed"):
                await _activity("consume")(
                    _context("consume"), _RefIn(ref=failing, other=stopped)
                )

        assert dirs
        assert not Path(dirs[0]).parent.exists()


class TestOverlappingCopies:
    async def test_directory_is_kept_while_a_file_inside_it_is_in_use(
        self, store: Any, tmp_path: Path
    ) -> None:
        tree, member = await _remote_tree_and_member(store, tmp_path)
        assert tree.local_path is not None and member.local_path is not None
        _consume_activity()
        activity_fn = _activity("consume")
        _gates["tree"], _entered["tree"] = asyncio.Event(), asyncio.Event()
        _gates["member"], _entered["member"] = asyncio.Event(), asyncio.Event()

        first = asyncio.create_task(
            activity_fn(_context("consume"), _RefIn(ref=tree, gate="tree"))
        )
        await _entered["tree"].wait()
        second = asyncio.create_task(
            activity_fn(_context("consume"), _RefIn(ref=member, gate="member"))
        )
        await _entered["member"].wait()

        _gates["tree"].set()
        await first
        assert Path(member.local_path).read_bytes() == _PAYLOAD

        _gates["member"].set()
        out = await second

        assert out.seen == _PAYLOAD.decode()
        assert not Path(member.local_path).exists()
        assert not Path(tree.local_path).exists()

    async def test_release_never_deletes_an_ancestor_of_a_live_copy(
        self, tmp_path: Path, store: Any
    ) -> None:
        outer = tmp_path / "outer"
        inner = outer / "inner.json"
        outer.mkdir()
        inner.write_bytes(_PAYLOAD)
        with file_ref_sync._input_leases_lock:
            file_ref_sync._lease(str(outer))
            file_ref_sync._lease(str(inner))

        await file_ref_sync.release_task_inputs([str(outer)])
        assert inner.read_bytes() == _PAYLOAD

        await file_ref_sync.release_task_inputs([str(inner)])
        assert not outer.exists()


def _lease_ref(path: Path) -> FileReference:
    return FileReference(
        local_path=str(path),
        storage_path=f"remote/{path.name}",
        is_durable=True,
        file_count=1,
    )


class TestOverlapEdges:
    async def test_existing_folder_holding_a_downloaded_file_is_not_deleted(
        self, tmp_path: Path, store: Any
    ) -> None:
        producer = tmp_path / "producer"
        producer.mkdir()
        (producer / "own.json").write_bytes(_PAYLOAD)
        downloaded = producer / "downloaded.json"

        member_leases = file_ref_sync.lease_task_inputs(_lease_ref(downloaded))
        downloaded.write_bytes(_PAYLOAD)
        folder_leases = file_ref_sync.lease_task_inputs(_lease_ref(producer))

        assert folder_leases == []
        await file_ref_sync.release_task_inputs(member_leases)
        assert not downloaded.exists()
        assert (producer / "own.json").read_bytes() == _PAYLOAD

    async def test_file_used_while_its_folder_waits_for_delete_is_kept(
        self, tmp_path: Path, store: Any
    ) -> None:
        folder = tmp_path / "tree"
        first, second = folder / "f.json", folder / "g.json"
        folder_leases = file_ref_sync.lease_task_inputs(_lease_ref(folder))
        folder.mkdir()
        first.write_bytes(_PAYLOAD)
        second.write_bytes(_PAYLOAD)
        first_leases = file_ref_sync.lease_task_inputs(_lease_ref(first))
        await file_ref_sync.release_task_inputs(folder_leases)
        assert folder.exists()

        second_leases = file_ref_sync.lease_task_inputs(_lease_ref(second))
        await file_ref_sync.release_task_inputs(first_leases)
        assert second.read_bytes() == _PAYLOAD

        await file_ref_sync.release_task_inputs(second_leases)
        assert not folder.exists()

    async def test_release_of_a_symlink_path_removes_only_the_link(
        self, tmp_path: Path, store: Any
    ) -> None:
        target = tmp_path / "elsewhere" / "data.json"
        target.parent.mkdir()
        target.write_bytes(_PAYLOAD)
        link = tmp_path / "inputs" / "data.json"
        leases = file_ref_sync.lease_task_inputs(_lease_ref(link))
        link.parent.mkdir()
        link.symlink_to(target)

        await file_ref_sync.release_task_inputs(leases)

        assert not os.path.lexists(link)
        assert target.read_bytes() == _PAYLOAD

    async def test_symlink_inside_a_downloaded_folder_keeps_its_target(
        self, tmp_path: Path, store: Any
    ) -> None:
        outside = tmp_path / "outside" / "data.json"
        outside.parent.mkdir()
        outside.write_bytes(_PAYLOAD)
        folder = tmp_path / "downloaded"
        folder_leases = file_ref_sync.lease_task_inputs(_lease_ref(folder))
        folder.mkdir()
        link = folder / "link.json"
        link.symlink_to(outside)
        link_leases = file_ref_sync.lease_task_inputs(_lease_ref(link))
        assert link_leases

        await file_ref_sync.release_task_inputs(link_leases)
        await file_ref_sync.release_task_inputs(folder_leases)

        assert not folder.exists()
        assert outside.read_bytes() == _PAYLOAD

    async def test_partial_download_files_are_deleted_with_the_copy(
        self, tmp_path: Path, store: Any
    ) -> None:
        target = tmp_path / "inputs" / "big.parquet"
        leases = file_ref_sync.lease_task_inputs(_lease_ref(target))
        staging = target.parent / ".sdk-partial"
        staging.mkdir(parents=True)
        (staging / "big.parquet.part").write_bytes(_PAYLOAD)
        (staging / "big.parquet.transfer-state").write_bytes(b"{}")

        await file_ref_sync.release_task_inputs(leases)

        assert not (staging / "big.parquet.part").exists()
        assert not (staging / "big.parquet.transfer-state").exists()

    async def test_failed_delete_is_retried_on_the_next_release(
        self, tmp_path: Path, store: Any
    ) -> None:
        stuck, other = tmp_path / "stuck.json", tmp_path / "other.json"
        stuck_leases = file_ref_sync.lease_task_inputs(_lease_ref(stuck))
        other_leases = file_ref_sync.lease_task_inputs(_lease_ref(other))
        stuck.write_bytes(_PAYLOAD)
        other.write_bytes(_PAYLOAD)
        real_rename = os.rename
        calls: list[str] = []

        def rename_once_fails(src: str, dst: str) -> None:
            if src == str(stuck) and not calls:
                calls.append(src)
                raise PermissionError("busy")
            real_rename(src, dst)

        with patch.object(file_ref_sync.os, "rename", rename_once_fails):
            await file_ref_sync.release_task_inputs(stuck_leases)
            assert stuck.exists()
            await file_ref_sync.release_task_inputs(other_leases)

        assert not stuck.exists() and not other.exists()

    async def test_nested_release_logs_no_false_warning(
        self, tmp_path: Path, store: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        folder = tmp_path / "tree"
        member = folder / "f.json"
        folder_leases = file_ref_sync.lease_task_inputs(_lease_ref(folder))
        folder.mkdir()
        member.write_bytes(_PAYLOAD)
        member_leases = file_ref_sync.lease_task_inputs(_lease_ref(member))

        with patch.object(file_ref_sync.logger, "warning") as warning:
            await file_ref_sync.release_task_inputs(folder_leases)
            assert member.exists()
            await file_ref_sync.release_task_inputs(member_leases)

        assert not folder.exists()
        warning.assert_not_called()


class TestPersistFailureStopsSiblings:
    async def test_other_uploads_stop_before_the_error_is_raised(
        self, tmp_path: Path, store: Any
    ) -> None:
        bad, slow = tmp_path / "bad.json", tmp_path / "slow.json"
        bad.write_bytes(_PAYLOAD)
        slow.write_bytes(_PAYLOAD)
        finished: list[str] = []

        async def fake_persist(
            _store: Any, ref: FileReference, **_: Any
        ) -> FileReference:
            if ref.local_path == str(bad):
                await asyncio.sleep(0)
                raise RuntimeError("upload failed")
            await asyncio.sleep(0.2)
            finished.append(ref.local_path or "")
            return ref

        with patch(
            "application_sdk.storage.reference.persist_file_reference", fake_persist
        ):
            with pytest.raises(RuntimeError, match="upload failed"):
                await file_ref_sync.persist_file_refs(
                    store,
                    [FileReference.from_local(bad), FileReference.from_local(slow)],
                )
            await asyncio.sleep(0.3)

        assert finished == []


class TestInterruptedDelete:
    async def test_second_cancel_still_removes_every_renamed_copy(
        self, tmp_path: Path, store: Any
    ) -> None:
        import threading

        paths = []
        for name in ("a.json", "b.json"):
            path = tmp_path / name
            path.write_bytes(_PAYLOAD)
            paths.append(str(path))
        with file_ref_sync._input_leases_lock:
            for path in paths:
                file_ref_sync._lease(path)
        gate = threading.Event()
        real = file_ref_sync._remove_tombstones

        def slow_remove(tombstones: list[str]) -> None:
            gate.wait(5)
            real(tombstones)

        with patch.object(file_ref_sync, "_remove_tombstones", slow_remove):
            release = asyncio.create_task(file_ref_sync.release_task_inputs(paths))
            await asyncio.sleep(0.05)
            release.cancel()
            await asyncio.sleep(0.01)
            release.cancel()
            with pytest.raises(asyncio.CancelledError):
                await release
            gate.set()
            for _ in range(100):
                if not list(tmp_path.glob("*.deleting")):
                    break
                await asyncio.sleep(0.01)

        assert not list(tmp_path.glob("*"))
