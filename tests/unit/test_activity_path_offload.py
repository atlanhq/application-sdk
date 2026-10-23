"""The activity path keeps the event loop free at each site FND-4084 offloaded.

Each test routes one blocking primitive through a
:class:`~tests.unit.loop_probe.LoopProbe` and checks two things while the call
that uses it runs:

* **Which thread ran it**: never the loop's.
* **The loop stayed free**: the probe's callback ran on the loop while the
  primitive was blocked. An earlier version measured the worst loop stall
  against a wall-clock bar; that measured the runner instead (a loaded
  Windows runner stalled an offloaded run past the bar with nothing on the
  loop) and failed one matrix cell at random (FND-4233).
"""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

from pydantic import BaseModel

from application_sdk._runtime.offload import run_in_thread
from application_sdk.contracts.types import FileReference
from tests.unit.loop_probe import LoopProbe
from tests.unit.private_stdlib import private_stdlib


class TestChunkedDownloadCheckpoint:
    async def test_checkpoints_do_not_fsync_on_the_loop(self, tmp_path: Path) -> None:
        """Each chunk's resume checkpoint is written on the loop; it used to fsync."""
        probe = LoopProbe()
        from application_sdk.storage.chunked import _transfer_state_path
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.ops import _put, download_file_chunked

        store = create_memory_store()
        content = bytes(range(256)) * 4
        await _put("lag/c.bin", content, store, normalize=False)
        path = tmp_path / "c.bin"
        real_fsync = os.fsync
        fsyncs: list[int] = []

        def slow_fsync(fd: int) -> None:
            fsyncs.append(threading.get_ident())
            probe.block()
            real_fsync(fd)

        real_save = None
        checkpoints: list[Path] = []

        def counting_save(state_path: Path, state: dict) -> None:
            checkpoints.append(state_path)
            assert real_save is not None
            real_save(state_path, state)

        import application_sdk.storage.chunked as chunked

        real_save = chunked._save_transfer_state
        with (
            private_stdlib(chunked, "os") as local_os,
            mock.patch.object(local_os, "fsync", slow_fsync),
            mock.patch.object(chunked, "_save_transfer_state", counting_save),
        ):
            with probe.bound():
                await download_file_chunked(
                    "lag/c.bin",
                    path,
                    store,
                    chunk_size_bytes=256,
                    max_concurrent_chunks=1,
                    normalize=False,
                    verify=False,
                    resume=True,
                )

        assert path.read_bytes() == content
        # Resume was on, so every chunk wrote a checkpoint...
        assert checkpoints == [_transfer_state_path(path)] * 4
        # ...and the only fsync left is the part file's, once, before publish.
        assert len(fsyncs) == 1
        assert threading.get_ident() not in fsyncs, "fsync ran on the loop thread"
        probe.assert_loop_stayed_free("download_file_chunked checkpoints")


class _SlowListStream:
    """obstore's ListStream: iterated synchronously, each page blocks the caller
    for its round trip; iterated with ``async for``, the wait is off the loop."""

    def __init__(self, batches: list[list[dict[str, str]]], probe: LoopProbe) -> None:
        self._batches = batches
        self._probe = probe
        self.iterated_synchronously = False

    def __iter__(self) -> Iterator[list[dict[str, str]]]:
        self.iterated_synchronously = True
        for batch in self._batches:
            self._probe.block()
            yield batch

    async def __aiter__(self) -> AsyncIterator[list[dict[str, str]]]:
        for batch in self._batches:
            await run_in_thread(self._probe.block)
            yield batch


class TestCleanupStorageListing:
    async def test_listing_pages_do_not_block_the_loop(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        probe = LoopProbe()
        from application_sdk.app.base import App, TaskStateAccessor
        from application_sdk.app.context import AppContext
        from application_sdk.constants import TRACKED_FILE_REFS_KEY
        from application_sdk.contracts.base import Input, Output
        from application_sdk.contracts.cleanup import StorageCleanupInput

        class _In(Input):
            value: str = ""

        class _Out(Output):
            result: str = ""

        class _LagApp(App):
            async def run(self, input: _In) -> _Out:
                return _Out()

        app = _LagApp()
        store = mock.MagicMock()
        ctx = AppContext(app_name="test", app_version="0.1.0", run_id="r1")
        ctx._storage = store
        app._context = ctx
        deleted: list[str] = []

        async def _delete(key: str, store: Any, normalize: bool = True) -> bool:
            deleted.append(key)
            return True

        listing = [
            [{"path": "file_refs/dir/part-0.parquet"}],
            [{"path": "file_refs/dir/part-1.parquet"}],
        ]
        streams: list[_SlowListStream] = []

        def _list(*_: Any, **__: Any) -> _SlowListStream:
            streams.append(_SlowListStream(listing, probe))
            return streams[-1]

        with mock.patch(
            "application_sdk.app.base._get_execution_id_from_task",
            return_value="wf-lag",
        ):
            TaskStateAccessor().set(
                TRACKED_FILE_REFS_KEY, [FileReference(storage_path="file_refs/dir/")]
            )
            try:
                with (
                    mock.patch(
                        "application_sdk.storage.ops._resolve_store",
                        return_value=store,
                    ),
                    mock.patch("application_sdk.storage.ops.delete", _delete),
                    mock.patch("obstore.list", side_effect=_list),
                ):
                    with probe.bound():
                        result = await app.cleanup_storage(StorageCleanupInput())
            finally:
                from application_sdk.app.base import _app_state, _app_state_lock

                with _app_state_lock:
                    _app_state.pop("wf-lag", None)

        assert deleted == [
            "file_refs/dir/part-0.parquet",
            "file_refs/dir/part-1.parquet",
        ]
        assert result.deleted_count == 2
        assert streams and not any(s.iterated_synchronously for s in streams)
        probe.assert_loop_stayed_free("cleanup_storage listing")


class TestMaterializeEmptyPrefix:
    async def test_counting_the_local_directory_does_not_block_the_loop(
        self, tmp_path: Path
    ) -> None:
        probe = LoopProbe()
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.reference import materialize_file_reference

        dest = tmp_path / "left-by-an-earlier-pass"
        dest.mkdir()
        (dest / "stale.json").write_bytes(b"{}")
        ref = FileReference(
            local_path=str(dest),
            storage_path="k/empty-prefix/",
            is_durable=True,
            file_count=0,
        )
        real_iterdir = Path.iterdir
        counted: list[int] = []

        def slow_iterdir(self: Path) -> Iterator[Path]:
            if self == dest:
                counted.append(threading.get_ident())
                probe.block()
            return real_iterdir(self)

        with mock.patch.object(Path, "iterdir", slow_iterdir):
            with probe.bound():
                result = await materialize_file_reference(create_memory_store(), ref)

        assert result is ref
        assert len(counted) == 1
        assert threading.get_ident() not in counted, "counted on the loop thread"
        probe.assert_loop_stayed_free("materialize_file_reference")


class TestAzureClientClose:
    async def test_executor_shutdown_does_not_block_the_loop(self) -> None:
        probe = LoopProbe()
        from application_sdk.clients.azure.client import AzureClient

        client = AzureClient()
        real_executor = client._executor
        assert real_executor is not None
        real_executor.shutdown(wait=True)
        executor = mock.MagicMock()
        shutdowns: list[int] = []

        def slow_shutdown(wait: bool = True) -> None:
            shutdowns.append(threading.get_ident())
            probe.block()

        executor.shutdown.side_effect = slow_shutdown
        client._executor = executor

        with probe.bound():
            await client.close()

        executor.shutdown.assert_called_once_with(wait=True)
        assert client._executor is None
        assert threading.get_ident() not in shutdowns, "shut down on the loop thread"
        probe.assert_loop_stayed_free("AzureClient.close")


class _SizedInput(BaseModel):
    ref: FileReference | None = None


class TestSizingInterceptorInputWalk:
    async def test_walking_file_references_does_not_block_the_loop(
        self, tmp_path: Path
    ) -> None:
        probe = LoopProbe()
        from dataclasses import dataclass, field

        from application_sdk.execution._temporal.interceptors.sizing import (
            _SizingActivityInboundInterceptor,
        )
        from application_sdk.observability.cgroup import ContainerTrace

        tree = tmp_path / "materialised"
        tree.mkdir()
        (tree / "a.parquet").write_bytes(b"x" * 100)
        real_walk = os.walk
        walks: list[int] = []

        def slow_walk(top: Any, *args: Any, **kwargs: Any) -> Any:
            walks.append(threading.get_ident())
            probe.block()
            return real_walk(top, *args, **kwargs)

        @dataclass
        class _Info:
            activity_type: str = "merge"
            task_queue: str = "q"
            workflow_type: str = "W"
            attempt: int = 1

        @dataclass
        class _ExecInput:
            args: list = field(default_factory=list)

        @contextlib.asynccontextmanager
        async def _tracker(**_: Any) -> AsyncIterator[ContainerTrace]:
            yield ContainerTrace()

        nxt = mock.AsyncMock()
        nxt.execute_activity = mock.AsyncMock(return_value="ok")
        interceptor = _SizingActivityInboundInterceptor(nxt, 1.0, frozenset({"merge"}))
        target = "application_sdk.execution._temporal.interceptors.sizing"
        with (
            mock.patch(f"{target}.activity") as act,
            mock.patch(f"{target}.track_container_usage", _tracker),
            mock.patch(f"{target}.record_observation") as record,
            private_stdlib(
                "application_sdk.observability.sizing_inputs", "os"
            ) as local_os,
            mock.patch.object(local_os, "walk", slow_walk),
        ):
            act.info.return_value = _Info()
            with probe.bound():
                got = await interceptor.execute_activity(
                    _ExecInput(
                        args=[
                            None,
                            _SizedInput(ref=FileReference(local_path=str(tree))),
                        ]
                    )
                )

        assert got == "ok"
        size = record.call_args[0][0].input_bytes
        assert size == 100
        assert walks and threading.get_ident() not in walks, "walked on the loop thread"
        probe.assert_loop_stayed_free("the sizing interceptor's input walk")
