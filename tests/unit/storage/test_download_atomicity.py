"""Atomic-publish behaviour of the download paths (CONNECT-1126).

Reproductions written red-first against the in-place ``O_TRUNC`` writes, plus
the failure-path guarantees the fix added: a 412/404 discard must not delete a
published destination, and a cancelled download must not strand its staging
file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import obstore
import pytest

from application_sdk.common._listing import PARTIAL_DIRNAME
from application_sdk.storage.chunked import _discard_transfer_state, _part_path
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.ops import _put, download_file, download_file_chunked
from application_sdk.storage.reference import _materialize_lock


@pytest.fixture
def store():
    return create_memory_store()


class TestDownloadAtomicPublish:
    """Downloads must publish atomically at the destination (CONNECT-1126).

    A ``FileReference.local_path`` is a deterministic function of
    (run, stage, entity), so concurrent activities of one run share the
    destination by construction. A reader — or a second downloader — must
    never observe a partial file at that path: at any instant the path holds
    either the previous complete content, the new complete content, or
    nothing.
    """

    async def test_download_never_exposes_partial_file(self, tmp_path) -> None:
        old = b'{"row": "old"}\n' * 4
        new = b'{"row": "new"}\n' * 4
        path = tmp_path / "records.json"
        path.write_bytes(old)

        wrote_first = asyncio.Event()
        may_finish = asyncio.Event()
        result = MagicMock()
        result.meta = {"size": len(new)}

        async def gen(min_chunk_size=None):
            yield new[: len(new) // 2]
            wrote_first.set()
            await may_finish.wait()
            yield new[len(new) // 2 :]

        result.stream = gen
        with patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(return_value=result),
        ):
            task = asyncio.create_task(
                download_file("k", path, MagicMock(), normalize=False, verify=False)
            )
            await wrote_first.wait()
            observed = path.read_bytes()
            may_finish.set()
            await task

        assert observed == old, f"reader-visible partial download: {observed!r}"
        assert path.read_bytes() == new

    async def test_interleaved_downloads_never_expose_nul_hole(self, tmp_path) -> None:
        """The production incident signature: download B opens (and, in-place,
        truncates) the destination while download A still holds a file offset
        past the truncation point. A's next write then leaves a zero-filled
        hole at the start of the file, and a JSONL reader fails at
        line 1 column 1 (char 0). Both downloads report success and A's
        streamed digest matches the object, so verification never fires.

        Chunks must exceed the 8 KiB ``io.BufferedWriter`` buffer so each
        write reaches the fd immediately, as production's 10 MiB chunks do.
        """
        content = b'{"row": 1}\n' * 2000
        half = len(content) // 2
        path = tmp_path / "records.json"

        a_wrote_first = asyncio.Event()
        a_may_finish = asyncio.Event()
        b_opened = asyncio.Event()
        b_may_write = asyncio.Event()

        def _result(gen):
            r = MagicMock()
            r.meta = {"size": len(content)}
            r.stream = gen
            return r

        async def gen_a(min_chunk_size=None):
            yield content[:half]
            a_wrote_first.set()
            await a_may_finish.wait()
            yield content[half:]

        async def gen_b(min_chunk_size=None):
            b_opened.set()
            await b_may_write.wait()
            yield content

        with patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(side_effect=[_result(gen_a), _result(gen_b)]),
        ):
            task_a = asyncio.create_task(
                download_file("k", path, MagicMock(), normalize=False, verify=False)
            )
            await a_wrote_first.wait()
            task_b = asyncio.create_task(
                download_file("k", path, MagicMock(), normalize=False, verify=False)
            )
            await b_opened.wait()
            a_may_finish.set()
            await task_a
            observed = path.read_bytes()
            b_may_write.set()
            await task_b

        assert observed == content, (
            f"reader-visible corruption after download A completed: {observed!r}"
        )
        assert path.read_bytes() == content

    async def test_chunked_download_never_exposes_preallocated_file(
        self, store, tmp_path
    ) -> None:
        content = bytes(range(20))
        await _put("atomic/c.bin", content, store, normalize=False)
        old = b"previous complete generation"
        path = tmp_path / "c.bin"
        path.write_bytes(old)

        real_get = obstore.get_async
        second_chunk_requested = asyncio.Event()
        may_serve = asyncio.Event()

        async def gated_get(st, key, **kw):
            rng = (kw.get("options") or {}).get("range")
            if rng and rng[0] == 10:
                second_chunk_requested.set()
                await may_serve.wait()
            return await real_get(st, key, **kw)

        with patch("application_sdk.storage.ops.obstore.get_async", new=gated_get):
            task = asyncio.create_task(
                download_file_chunked(
                    "atomic/c.bin",
                    path,
                    store,
                    chunk_size_bytes=10,
                    max_concurrent_chunks=1,
                    normalize=False,
                    verify=False,
                )
            )
            await second_chunk_requested.wait()
            observed = path.read_bytes()
            may_serve.set()
            await task

        assert (
            observed == old
        ), f"reader-visible preallocated/partial file: {observed!r}"
        assert path.read_bytes() == content


class TestDiscardKeepsDestination:
    """A 412/404 discard removes only in-flight state — never the published file."""

    def test_discard_transfer_state_preserves_published_destination(
        self, tmp_path
    ) -> None:
        dest = tmp_path / "records.json"
        dest.write_bytes(b"previous complete generation")
        part = _part_path(dest)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"half a new generation")
        state = Path(str(dest) + ".transfer-state")
        state.write_bytes(b"{}")

        _discard_transfer_state(dest)

        assert dest.read_bytes() == b"previous complete generation"
        assert not part.exists()
        assert not state.exists()


class TestCancellationCleanup:
    """A cancelled download must not strand a uniquely-named staging file."""

    async def test_cancelled_download_leaves_no_staging_orphan(self, tmp_path) -> None:
        path = tmp_path / "records.json"
        entered = asyncio.Event()

        result = MagicMock()
        result.meta = {"size": 8}

        async def gen(min_chunk_size=None):
            yield b"1234"
            entered.set()
            await asyncio.sleep(60)
            yield b"5678"

        result.stream = gen
        with patch(
            "application_sdk.storage.ops.obstore.get_async",
            new=AsyncMock(return_value=result),
        ):
            task = asyncio.create_task(
                download_file("k", path, MagicMock(), normalize=False, verify=False)
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        staging = path.parent / PARTIAL_DIRNAME
        assert not path.exists()
        assert not staging.exists() or list(staging.iterdir()) == []


class TestMaterializeLockRegistry:
    """The per-path lock must work from more than one event loop."""

    def test_lock_usable_across_two_event_loops(self, tmp_path) -> None:
        target = str(tmp_path / "records.json")

        async def contend() -> None:
            lock = _materialize_lock(target)
            async with lock:
                waiter = asyncio.ensure_future(lock.acquire())
                await asyncio.sleep(0)
                assert not waiter.done()
                waiter.cancel()

        asyncio.run(contend())
        asyncio.run(contend())
