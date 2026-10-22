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
    file_ref_sync._input_leases.clear()
    assert not leaked
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
