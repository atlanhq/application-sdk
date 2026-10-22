"""Task inputs the wrapper downloaded are deleted when the task ends (FND-4310).

``create_activity_from_task`` materialises durable ``FileReference`` inputs
onto the pod before the task runs. Every download goes into one folder per task
(``<TEMPORARY_PATH>/.sdk-task-inputs/<id>/<storage_path>``), deleted once the
task's outputs are persisted. These tests drive a real activity against an
in-memory store and check what is left on local disk afterwards.

The App classes are defined inside each test because
``tests/unit/execution/conftest.py`` resets the registries around every test.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any
from unittest.mock import patch

import pytest

from application_sdk import constants
from application_sdk.app.base import App, _app_state, _app_state_lock
from application_sdk.app.registry import TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.storage import DeclaredFile, UploadRefsInput
from application_sdk.contracts.types import FileReference, Lazy, StoreTarget
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
)
from application_sdk.infrastructure import context as infra_context
from application_sdk.infrastructure.context import InfrastructureContext
from application_sdk.storage import file_ref_sync, ops
from application_sdk.storage.batch import list_keys
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.reference import fetch, persist_file_reference
from tests.unit.private_stdlib import private_stdlib

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


class _RefOut(Output, allow_unbounded_fields=True):
    out_ref: FileReference | None = None


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
        async def write_beside(self, input: _RefIn) -> _RefOut:
            assert input.ref is not None and input.ref.local_path is not None
            output = Path(input.ref.local_path).parent / "derived.json"
            output.write_bytes(_PAYLOAD)
            return _RefOut(out_ref=FileReference.from_local(output))

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
async def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Any]:
    memory = create_memory_store()
    monkeypatch.setattr(
        infra_context, "_infrastructure", InfrastructureContext(storage=memory)
    )
    monkeypatch.setattr(constants, "TEMPORARY_PATH", str(tmp_path / "work"))
    _gates.clear()
    _entered.clear()
    _failures.clear()
    with _app_state_lock:
        _app_state.clear()
    ops._warned_task_input_keys.clear()
    yield memory
    await asyncio.gather(*list(file_ref_sync._pending_removals))
    assert _task_folders(tmp_path) == []
    with _app_state_lock:
        _app_state.clear()


async def _gone(path: str | Path) -> bool:
    for _ in range(200):
        if not os.path.lexists(path):
            return True
        await asyncio.sleep(0.01)
    return False


def _task_folders(tmp_path: Path) -> list[Path]:
    root = tmp_path / "work" / ".sdk-task-inputs"
    return sorted(root.iterdir()) if root.exists() else []


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
    async def test_single_file_input_goes_to_the_task_folder_and_is_deleted(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        assert ref.local_path is not None and ref.storage_path is not None
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.seen == _PAYLOAD.decode()
        assert out.path.endswith(ref.storage_path)
        assert "/.sdk-task-inputs/" in out.path
        assert await _gone(out.path)
        assert not Path(ref.local_path).exists()

    async def test_directory_input_is_deleted_after_the_task(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_dir_ref(store, tmp_path)
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.seen == (_PAYLOAD * 2).decode()
        assert await _gone(out.path)

    async def test_input_without_local_path_is_deleted_after_the_task(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        ref = ref.model_copy(update={"local_path": None})
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.seen == _PAYLOAD.decode()
        assert out.path and await _gone(out.path)

    async def test_failed_attempt_deletes_and_retry_downloads_again(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        _consume_activity()
        activity_fn = _activity("consume")

        with pytest.raises(Exception, match="first attempt fails"):
            await activity_fn(_context("consume"), _RefIn(ref=ref, fail=True))
        assert _failures and await _gone(_failures[0])

        out = await activity_fn(_context("consume"), _RefIn(ref=ref, fail=True))

        assert out.seen == _PAYLOAD.decode()
        assert out.path != _failures[0]
        assert await _gone(out.path)

    async def test_rewritten_directory_never_hands_a_ref_new_content(
        self, store: Any, tmp_path: Path
    ) -> None:
        producer = tmp_path / "producer" / "tree"
        producer.mkdir(parents=True)
        (producer / "data.json").write_bytes(b"first")
        first = await persist_file_reference(store, FileReference.from_local(producer))
        (producer / "data.json").write_bytes(b"second")
        await persist_file_reference(store, FileReference.from_local(producer))
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=first))

        assert out.seen == "first"
        assert out.path != str(producer)

    async def test_changed_producer_directory_is_downloaded_instead(
        self, store: Any, tmp_path: Path
    ) -> None:
        producer = tmp_path / "producer" / "tree"
        producer.mkdir(parents=True)
        (producer / "one.json").write_bytes(_PAYLOAD)
        ref = await persist_file_reference(store, FileReference.from_local(producer))
        (producer / "extra.json").write_bytes(_PAYLOAD)
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.path != str(producer)
        assert out.seen == _PAYLOAD.decode()
        assert (producer / "extra.json").exists()

    async def test_storage_path_cannot_leave_the_task_folder(self) -> None:
        root = "/work/.sdk-task-inputs/abc"
        for key in ("../../escape.json", "/../../etc/passwd", "", "."):
            ref = FileReference(storage_path=key or None, is_durable=True)
            target = file_ref_sync._download_target(root, ref, 0)
            assert target.startswith(root + "/0/")
        escaped = FileReference(storage_path="../out.parquet", is_durable=True)
        assert file_ref_sync._download_target(root, escaped, 0).endswith(".parquet")
        ref = FileReference(
            storage_path="run/transformed/table/x.json", is_durable=True
        )
        assert file_ref_sync._download_target(root, ref, 3) == (
            root + "/3/run/transformed/table/x.json"
        )

    async def test_two_objects_never_share_a_path(
        self, store: Any, tmp_path: Path
    ) -> None:
        refs = [
            FileReference(storage_path="same/key.json", is_durable=True),
            FileReference(
                storage_path="same/key.json",
                is_durable=True,
                store=StoreTarget.UPSTREAM,
            ),
            FileReference(storage_path="nested", is_durable=True),
            FileReference(storage_path="nested/child.json", is_durable=True),
        ]
        seen: list[str] = []

        async def fake_materialize(_store: Any, ref: FileReference) -> FileReference:
            seen.append(ref.local_path or "")
            return ref

        with patch(
            "application_sdk.storage.reference.materialize_file_reference",
            fake_materialize,
        ):
            await file_ref_sync.materialize_file_refs(
                store, refs, download_dir=str(tmp_path / "dl")
            )

        assert len(set(seen)) == 4
        for path in seen:
            assert not any(
                other != path and other.startswith(path + "/") for other in seen
            )

    async def test_non_empty_directory_is_not_reused_for_an_empty_prefix_ref(
        self, store: Any, tmp_path: Path
    ) -> None:
        tree = await _remote_dir_ref(store, tmp_path)
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "other.json").write_bytes(_PAYLOAD)
        ref = tree.model_copy(update={"local_path": str(shared), "file_count": 0})

        out = await file_ref_sync.materialize_file_refs(
            store, ref, download_dir=str(tmp_path / "dl")
        )

        assert out.local_path != str(shared)
        assert sorted(p.name for p in shared.iterdir()) == ["other.json"]

    async def test_empty_prefix_ref_keeps_its_existing_directory(
        self, store: Any, tmp_path: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        ref = FileReference(
            local_path=str(empty),
            storage_path="nothing/here",
            is_durable=True,
            file_count=0,
        )

        out = await file_ref_sync.materialize_file_refs(
            store, ref, download_dir=str(tmp_path / "dl")
        )

        assert out.local_path == str(empty)
        assert empty.is_dir()


class TestConcurrentTasksOnOnePod:
    async def test_each_task_gets_its_own_copy(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
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
        first_out = await first
        _gates["second"].set()
        second_out = await second

        assert first_out.seen == second_out.seen == _PAYLOAD.decode()
        assert first_out.path != second_out.path


class TestFilesTheWrapperDidNotDownloadAreKept:
    async def test_current_local_copy_is_used_in_place_and_kept(
        self, store: Any, tmp_path: Path
    ) -> None:
        """Same-pod hand-off: the producer's verified copy is already at the path."""
        producer = tmp_path / "producer.json"
        producer.write_bytes(_PAYLOAD)
        ref = await persist_file_reference(store, FileReference.from_local(producer))
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.path == str(producer)
        assert producer.read_bytes() == _PAYLOAD

    async def test_stale_local_copy_is_not_overwritten_or_deleted(
        self, store: Any, tmp_path: Path
    ) -> None:
        producer = tmp_path / "producer.json"
        producer.write_bytes(_PAYLOAD)
        ref = await persist_file_reference(store, FileReference.from_local(producer))
        _sidecar(str(producer)).unlink()
        _consume_activity()

        out = await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        assert out.seen == _PAYLOAD.decode()
        assert out.path != str(producer)
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


class TestOutputsSurviveTheInputFolder:
    async def test_output_written_inside_the_input_folder_is_persisted_first(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
        _consume_activity()

        out = await _activity("write_beside")(_context("write_beside"), _RefIn(ref=ref))

        assert out.out_ref is not None and out.out_ref.is_durable
        assert out.out_ref.local_path is not None
        assert await _gone(out.out_ref.local_path)
        fetched = await fetch(out.out_ref.model_copy(update={"local_path": None}))
        assert _read(fetched.local_path or "") == _PAYLOAD
        Path(fetched.local_path or "").unlink()


class TestFrameworkTasksStillWork:
    async def test_upload_refs_delivers_then_drops_the_downloaded_input(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)
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

    async def test_cleanup_files_runs_after_inputs_were_deleted(
        self, store: Any, tmp_path: Path
    ) -> None:
        from application_sdk.contracts.cleanup import CleanupInput

        ref = await _remote_file_ref(store, tmp_path)
        _consume_activity()
        await _activity("consume")(_context("consume"), _RefIn(ref=ref))

        leftover = tmp_path / "app-scratch.json"
        leftover.write_bytes(_PAYLOAD)
        with patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-fnd4310",
        ):
            out = await _activity("cleanup_files")(
                _context("cleanup_files"), CleanupInput(extra_paths=[str(leftover)])
            )

        assert out.path_results[str(leftover)] is True
        assert not leftover.exists()


class TestLeftoversAndWarnings:
    async def test_sweep_deletes_only_folders_whose_owner_is_dead(
        self, tmp_path: Path, store: Any
    ) -> None:
        root = tmp_path / "work" / ".sdk-task-inputs"
        live = await file_ref_sync.open_task_input_dir()
        (Path(live.path) / "0").mkdir()
        (Path(live.path) / "0" / "x.json").write_bytes(_PAYLOAD)
        dead = root / "dead"
        (dead / "0").mkdir(parents=True)
        (dead / file_ref_sync._LOCK_NAME).write_bytes(b"")
        stray = root / "stray-file"
        stray.write_bytes(_PAYLOAD)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (root / "link").symlink_to(elsewhere)

        await file_ref_sync.sweep_dead_task_inputs()

        assert not dead.exists()
        assert (Path(live.path) / "0" / "x.json").read_bytes() == _PAYLOAD
        assert stray.exists() and elsewhere.exists()
        stray.unlink()
        (root / "link").unlink()
        file_ref_sync.remove_task_input_dir(live)

    async def test_key_from_a_task_download_path_warns(
        self, tmp_path: Path, store: Any
    ) -> None:
        from application_sdk.storage import ops

        path = str(tmp_path / "work" / ".sdk-task-inputs" / "id" / "0" / "k.json")
        with patch.object(ops.logger, "warning") as warning:
            key = ops.normalize_key(path)
            ops.normalize_key(path)

        assert key.startswith(".sdk-task-inputs/")
        warning.assert_called_once()
        assert key in warning.call_args.args

    async def test_removal_runs_on_its_own_daemon_thread(
        self, tmp_path: Path, store: Any
    ) -> None:
        import threading

        owned = await file_ref_sync.open_task_input_dir()
        threads: list[tuple[str, bool]] = []
        real_remove = file_ref_sync._remove_owned

        def recording_remove(path: str, fd: int | None) -> bool:
            current = threading.current_thread()
            threads.append((current.name, current.daemon))
            return real_remove(path, fd)

        with patch.object(file_ref_sync, "_remove_owned", recording_remove):
            file_ref_sync.remove_task_input_dir(owned)
            await asyncio.gather(*list(file_ref_sync._pending_removals))

        assert threads == [("sdk-task-input-removal", True)]
        assert not Path(owned.path).exists()

    async def test_same_object_in_two_task_folders_warns_once(
        self, tmp_path: Path, store: Any
    ) -> None:
        root = tmp_path / "work" / ".sdk-task-inputs"
        with patch.object(ops.logger, "warning") as warning:
            ops.normalize_key(str(root / "aaa" / "0" / "run" / "k.json"))
            ops.normalize_key(str(root / "bbb" / "2" / "run" / "k.json"))

        warning.assert_called_once()

    async def test_different_keys_warn_once_per_process(
        self, tmp_path: Path, store: Any
    ) -> None:
        root = tmp_path / "work" / ".sdk-task-inputs"
        with patch.object(ops.logger, "warning") as warning:
            ops.normalize_key(str(root / "aaa" / "0" / "one.json"))
            ops.normalize_key(str(root / "bbb" / "0" / "two.json"))

        warning.assert_called_once()

    async def test_ordinary_key_does_not_warn(self, tmp_path: Path, store: Any) -> None:
        with patch.object(ops.logger, "warning") as warning:
            ops.normalize_key(str(tmp_path / "work" / "artifacts" / "k.json"))
            ops.normalize_key("artifacts/apps/x/k.json")

        warning.assert_not_called()

    async def test_upload_with_ref_does_not_warn(
        self, tmp_path: Path, store: Any
    ) -> None:
        from application_sdk.contracts.storage import UploadInput

        ref = await _remote_file_ref(store, tmp_path)
        local = tmp_path / "work" / ".sdk-task-inputs" / "id" / "0" / "out.json"
        local.parent.mkdir(parents=True)
        local.write_bytes(_PAYLOAD)
        _consume_activity()

        with patch.object(ops.logger, "warning") as warning:
            await _activity("upload")(
                _context("upload"), UploadInput(local_path=str(local), ref=ref)
            )

        warning.assert_not_called()
        shutil.rmtree(tmp_path / "work" / ".sdk-task-inputs" / "id")


class TestFolderLifecycle:
    async def test_sweep_between_lock_file_and_lock_cannot_delete_a_new_folder(
        self, tmp_path: Path, store: Any
    ) -> None:
        swept: list[bool] = []

        with private_stdlib(file_ref_sync, "fcntl") as local_fcntl:
            real_flock = local_fcntl.flock

            def flock_after_a_sweep(fd: int, op: int) -> None:
                if op == local_fcntl.LOCK_EX and not swept:
                    swept.append(True)
                    file_ref_sync._sweep_dead_task_inputs()
                real_flock(fd, op)

            local_fcntl.flock = flock_after_a_sweep
            owned = await file_ref_sync.open_task_input_dir()

        assert swept
        assert (Path(owned.path) / file_ref_sync._LOCK_NAME).exists()
        file_ref_sync.remove_task_input_dir(owned)

    async def test_folder_without_lock_file_is_swept_but_creating_one_is_not(
        self, tmp_path: Path, store: Any
    ) -> None:
        root = tmp_path / "work" / ".sdk-task-inputs"
        lockless = root / "lockless"
        (lockless / "0").mkdir(parents=True)
        creating = root / ".abc.creating"
        creating.mkdir()

        await file_ref_sync.sweep_dead_task_inputs()

        assert not lockless.exists()
        assert creating.exists()
        creating.rmdir()

    async def test_lock_is_released_when_the_delete_cannot_start(
        self, tmp_path: Path, store: Any
    ) -> None:
        owned = await file_ref_sync.open_task_input_dir()

        class _Closed:
            def run_in_executor(self, *_: Any) -> Any:
                raise RuntimeError("executor shut down")

        with private_stdlib(file_ref_sync, "asyncio") as local_asyncio:
            local_asyncio.get_running_loop = lambda: _Closed()
            file_ref_sync.remove_task_input_dir(owned)

        with pytest.raises(OSError):
            os.fstat(owned.lock_fd)
        await file_ref_sync.sweep_dead_task_inputs()
        assert not Path(owned.path).exists()

    async def test_cancel_during_creation_still_deletes_the_folder(
        self, tmp_path: Path, store: Any
    ) -> None:
        import threading

        gate = threading.Event()
        entered = threading.Event()
        created: list[int] = []
        real_create = file_ref_sync._create_owned_dir

        def slow_create(path: str) -> int:
            entered.set()
            gate.wait(10)
            fd = real_create(path)
            created.append(fd)
            return fd

        with patch.object(file_ref_sync, "_create_owned_dir", slow_create):
            opening = asyncio.create_task(file_ref_sync.open_task_input_dir())
            while not entered.is_set():
                await asyncio.sleep(0.001)
            opening.cancel()
            with pytest.raises(asyncio.CancelledError):
                await opening
            gate.set()
            while not created:
                await asyncio.sleep(0.001)
        for _ in range(500):
            if not file_ref_sync._pending_removals and not _task_folders(tmp_path):
                break
            await asyncio.sleep(0.01)

        assert _task_folders(tmp_path) == []
        with pytest.raises(OSError):
            os.fstat(created[0])

    @pytest.mark.filterwarnings(
        "ignore:This process .* is multi-threaded:DeprecationWarning"
    )
    async def test_forked_child_closes_inherited_locks(
        self, tmp_path: Path, store: Any
    ) -> None:
        owned = await file_ref_sync.open_task_input_dir()

        spare = await file_ref_sync.open_task_input_dir()
        file_ref_sync.remove_task_input_dir(spare)
        await asyncio.gather(*list(file_ref_sync._pending_removals))
        parent_queue = file_ref_sync._removal_queue
        assert file_ref_sync._removal_threads

        file_ref_sync._removal_thread_guard.acquire()
        try:
            pid = os.fork()
        finally:
            if pid != 0:
                file_ref_sync._removal_thread_guard.release()
        if pid == 0:
            fresh = (
                not file_ref_sync._removal_threads
                and file_ref_sync._removal_queue is not parent_queue
                and not file_ref_sync._pending_removals
                and file_ref_sync._removal_thread_guard.acquire(blocking=False)
            )
            try:
                os.fstat(owned.lock_fd)
            except OSError:
                os._exit(0 if fresh else 2)
            os._exit(1)
        _, status = os.waitpid(pid, 0)

        assert os.waitstatus_to_exitcode(status) == 0
        os.fstat(owned.lock_fd)
        file_ref_sync.remove_task_input_dir(owned)

    async def test_failed_lock_file_leaves_no_creating_folder(
        self, tmp_path: Path, store: Any
    ) -> None:
        real_open = os.open

        def no_space(path: str, *args: Any, **kwargs: Any) -> int:
            if str(path).endswith(file_ref_sync._LOCK_NAME):
                raise OSError(28, "No space left on device")
            return real_open(path, *args, **kwargs)

        with private_stdlib(file_ref_sync, "os") as local_os:
            local_os.open = no_space
            with pytest.raises(OSError):
                await file_ref_sync.open_task_input_dir()

        root = tmp_path / "work" / ".sdk-task-inputs"
        assert not root.exists() or list(root.iterdir()) == []

    async def test_unfinished_delete_retries_only_that_folder(
        self, tmp_path: Path, store: Any
    ) -> None:
        owned = await file_ref_sync.open_task_input_dir()
        (Path(owned.path) / "0").mkdir()
        other = tmp_path / "work" / ".sdk-task-inputs" / "other-dead"
        other.mkdir()

        def gives_up(path: str, fd: int | None) -> bool:
            if fd is not None:
                file_ref_sync._close_lock(fd)
            return False

        real_remove = file_ref_sync._remove_owned
        calls: list[int | None] = []

        def first_gives_up(path: str, fd: int | None) -> bool:
            calls.append(fd)
            if len(calls) == 1:
                return gives_up(path, fd)
            return real_remove(path, fd)

        with patch.object(file_ref_sync, "_remove_owned", first_gives_up):
            file_ref_sync.remove_task_input_dir(owned)
            await asyncio.gather(*list(file_ref_sync._pending_removals))

        assert len(calls) == 2
        assert not Path(owned.path).exists()
        assert other.exists()
        other.rmdir()

    async def test_no_folder_for_inputs_that_are_never_downloaded(
        self, tmp_path: Path, store: Any
    ) -> None:
        ref = await _remote_file_ref(store, tmp_path)

        assert file_ref_sync.needs_task_input_dir(_RefIn(ref=ref))
        assert not file_ref_sync.needs_task_input_dir(_RefIn(lazy_ref=ref))
        assert not file_ref_sync.needs_task_input_dir(
            _RefIn(ref=ref.model_copy(update={"auto_materialize": False}))
        )

    async def test_shutdown_wait_is_bounded(self, tmp_path: Path, store: Any) -> None:
        import threading

        owned = await file_ref_sync.open_task_input_dir()
        gate = threading.Event()
        real_remove = file_ref_sync._remove_owned

        def slow_remove(path: str, fd: int | None) -> None:
            gate.wait(10)
            real_remove(path, fd)

        with patch.object(file_ref_sync, "_remove_owned", slow_remove):
            file_ref_sync.remove_task_input_dir(owned)
            with patch.object(file_ref_sync.logger, "warning") as warning:
                await file_ref_sync.wait_for_task_input_removals(0.05)
            gate.set()
            await asyncio.gather(*list(file_ref_sync._pending_removals))

        warning.assert_called_once()


class TestWorkerWiring:
    async def test_worker_start_sweeps_and_exit_stops_cleanup(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.execution._temporal import worker as worker_module

        fake = MagicMock()
        fake._start_event_params = {}
        fake._shutdown_drain_delay_seconds = 0
        fake._start_metrics_push = AsyncMock()
        fake._stop_metrics_push = AsyncMock()
        fake._drain_sizing = AsyncMock()
        fake._worker.__aenter__ = AsyncMock()
        fake._worker.__aexit__ = AsyncMock()
        fake._worker.run = AsyncMock()

        with (
            patch.object(worker_module, "_emit_worker_start_event", AsyncMock()),
            patch.object(worker_module, "_start_dead_task_inputs_sweep") as sweep,
            patch.object(
                worker_module, "_stop_task_input_cleanup", AsyncMock()
            ) as stop,
        ):
            await worker_module.AppWorker.__aenter__(fake)
            await worker_module.AppWorker.__aexit__(fake, None)
            await worker_module.AppWorker.run(fake)

        assert sweep.call_count == 2
        assert stop.await_count == 2

    async def test_shutdown_uses_one_deadline(self) -> None:
        from unittest.mock import AsyncMock

        from application_sdk.execution._temporal import worker as worker_module

        clock = iter([100.0, 100.0, 100.3])
        timeouts: list[float] = []

        class _Loop:
            def time(self) -> float:
                return next(clock)

        async def fake_wait(aws: Any, timeout: float) -> tuple[set[Any], set[Any]]:
            timeouts.append(timeout)
            return set(), set()

        sweep = asyncio.ensure_future(asyncio.sleep(0))
        worker_module._background_sweeps.add(sweep)
        removals = AsyncMock()
        try:
            with (
                private_stdlib(worker_module, "asyncio") as local_asyncio,
                patch.object(file_ref_sync, "wait_for_task_input_removals", removals),
                patch.object(
                    worker_module, "_TASK_INPUT_CLEANUP_SHUTDOWN_SECONDS", 0.5
                ),
            ):
                local_asyncio.get_running_loop = lambda: _Loop()
                local_asyncio.wait = fake_wait
                await worker_module._stop_task_input_cleanup()
        finally:
            await sweep
            worker_module._background_sweeps.discard(sweep)

        assert timeouts == [0.5]
        assert removals.await_args.args[0] == pytest.approx(0.2)


class TestFailedTransfersLeaveNothing:
    async def test_other_downloads_stop_before_cleanup_runs(
        self, store: Any, tmp_path: Path
    ) -> None:
        failing = await _remote_file_ref(store, tmp_path)
        slow = await _remote_dir_ref(store, tmp_path)
        release = asyncio.Event()
        written: list[str] = []
        _consume_activity()

        async def fake_materialize(_store: Any, ref: FileReference) -> FileReference:
            if ref.storage_path == failing.storage_path:
                await asyncio.sleep(0)
                raise RuntimeError("download failed")
            await release.wait()
            Path(ref.local_path or "").mkdir(parents=True, exist_ok=True)
            written.append(ref.local_path or "")
            return ref

        asyncio.get_running_loop().call_later(0.3, release.set)
        with patch(
            "application_sdk.storage.reference.materialize_file_reference",
            fake_materialize,
        ):
            with pytest.raises(Exception, match="download failed"):
                await _activity("consume")(
                    _context("consume"), _RefIn(ref=failing, other=slow)
                )
            await asyncio.sleep(0.4)

        assert written == []

    async def test_download_thread_finishes_before_cleanup_runs(
        self, store: Any, tmp_path: Path
    ) -> None:
        import threading

        from application_sdk._runtime.offload import run_in_thread

        failing = await _remote_file_ref(store, tmp_path)
        slow = await _remote_dir_ref(store, tmp_path)
        gate = threading.Event()
        started = threading.Event()
        _consume_activity()

        def write_when_released(target: str) -> None:
            started.set()
            gate.wait(10)
            Path(target).mkdir(parents=True, exist_ok=True)
            (Path(target) / "late.json").write_bytes(_PAYLOAD)

        async def fake_materialize(_store: Any, ref: FileReference) -> FileReference:
            if ref.storage_path == failing.storage_path:
                while not started.is_set():
                    await asyncio.sleep(0.001)
                raise RuntimeError("download failed")
            await run_in_thread(write_when_released, ref.local_path or "")
            return ref

        with patch(
            "application_sdk.storage.reference.materialize_file_reference",
            fake_materialize,
        ):
            run = asyncio.create_task(
                _activity("consume")(
                    _context("consume"), _RefIn(ref=failing, other=slow)
                )
            )
            while not started.is_set():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.05)
            assert not run.done()
            gate.set()
            with pytest.raises(Exception, match="download failed"):
                await run

    async def test_cancel_during_the_wait_still_drains_the_threads(
        self, tmp_path: Path, store: Any
    ) -> None:
        import threading

        from application_sdk._runtime.offload import run_in_thread

        order: list[str] = []
        gate = threading.Event()
        started = threading.Event()
        unwind = asyncio.Event()
        refs = [
            FileReference(
                local_path=str(tmp_path / name),
                storage_path=f"remote/{name}",
                is_durable=True,
                file_count=1,
            )
            for name in ("fails.json", "slow.json")
        ]

        def slow_thread() -> None:
            started.set()
            gate.wait(10)
            order.append("thread")

        async def fake_materialize(_store: Any, ref: FileReference) -> FileReference:
            if ref.storage_path == "remote/fails.json":
                while not started.is_set():
                    await asyncio.sleep(0.001)
                raise RuntimeError("download failed")
            try:
                await run_in_thread(slow_thread)
            except asyncio.CancelledError:
                await unwind.wait()
                raise
            return ref

        with patch(
            "application_sdk.storage.reference.materialize_file_reference",
            fake_materialize,
        ):
            run = asyncio.create_task(file_ref_sync.materialize_file_refs(store, refs))
            run.add_done_callback(lambda _: order.append("caller"))
            while not started.is_set():
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.05)
            run.cancel()
            await asyncio.sleep(0.01)
            unwind.set()
            await asyncio.sleep(0.05)
            assert not run.done()
            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await run

        assert order == ["thread", "caller"]

    async def test_failed_upload_stops_the_other_uploads(
        self, tmp_path: Path, store: Any
    ) -> None:
        bad, slow = tmp_path / "bad.json", tmp_path / "slow.json"
        bad.write_bytes(_PAYLOAD)
        slow.write_bytes(_PAYLOAD)
        finished: list[str] = []
        release = asyncio.Event()

        async def fake_persist(
            _store: Any, ref: FileReference, **_: Any
        ) -> FileReference:
            if ref.local_path == str(bad):
                await asyncio.sleep(0)
                raise RuntimeError("upload failed")
            await release.wait()
            finished.append(ref.local_path or "")
            return ref

        asyncio.get_running_loop().call_later(0.3, release.set)
        with patch(
            "application_sdk.storage.reference.persist_file_reference", fake_persist
        ):
            with pytest.raises(RuntimeError, match="upload failed"):
                await file_ref_sync.persist_file_refs(
                    store,
                    [FileReference.from_local(bad), FileReference.from_local(slow)],
                )
            await asyncio.sleep(0.4)

        assert finished == []
