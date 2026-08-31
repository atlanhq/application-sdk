"""Atomic-publish behaviour of the download paths (CONNECT-1126).

Reproductions written red-first against the in-place ``O_TRUNC`` writes, plus
the failure-path guarantees the fix added: a 412/404 discard must not delete a
published destination, and a cancelled download must not strand its staging
file.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import obstore
import pytest

from application_sdk.common._listing import PARTIAL_DIRNAME
from application_sdk.storage.chunked import (
    _TRANSFER_LOCKS,
    _discard_transfer_state,
    _part_path,
    _transfer_state_path,
)
from application_sdk.storage.errors import StorageError
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

        assert (
            observed == content
        ), f"reader-visible corruption after download A completed: {observed!r}"
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
        state = _transfer_state_path(dest)
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


class TestTransferLockRegistry:
    """The transfer-layer lock guards the resource, not just the materialise path."""

    async def test_transfer_and_materialize_locks_are_distinct(self, tmp_path) -> None:
        """Materialise holds its guard and then calls into the transfer layer,
        so the two registries must hand out different lock objects for one
        destination — same object would be a self-deadlock."""
        target = str(tmp_path / "records.json")
        assert _TRANSFER_LOCKS.lock(target) is not _materialize_lock(target)

    async def test_guard_releases_on_exception(self, tmp_path) -> None:
        target = str(tmp_path / "records.json")
        with pytest.raises(RuntimeError):
            async with _TRANSFER_LOCKS.guard(target):
                raise RuntimeError("boom")
        assert not _TRANSFER_LOCKS.lock(target).locked()

    async def test_concurrent_chunked_downloads_serialise_on_destination(
        self, store, tmp_path
    ) -> None:
        """Two concurrent ``download_file_chunked`` calls on one destination
        share the deterministic ``.part`` + checkpoint, so the second must not
        touch the store (or the staging files) until the first has published.
        Every public entry point — ``download_prefix``, ``batch``, the
        ``common.utils`` helper — reaches this code path unlocked, so the
        exclusion has to live in the primitive itself."""
        content = bytes(range(20))
        await _put("lock/c.bin", content, store, normalize=False)
        path = tmp_path / "c.bin"

        real_get = obstore.get_async
        gets: list[object] = []
        second_chunk_requested = asyncio.Event()
        may_serve = asyncio.Event()

        async def gated_get(st, key, **kw):
            gets.append(kw.get("options"))
            rng = (kw.get("options") or {}).get("range")
            if rng and rng[0] == 10:
                second_chunk_requested.set()
                await may_serve.wait()
            return await real_get(st, key, **kw)

        def _download():
            return download_file_chunked(
                "lock/c.bin",
                path,
                store,
                chunk_size_bytes=10,
                max_concurrent_chunks=1,
                normalize=False,
                verify=False,
            )

        with patch("application_sdk.storage.ops.obstore.get_async", new=gated_get):
            task_a = asyncio.create_task(_download())
            await second_chunk_requested.wait()
            gets_before_b = len(gets)
            task_b = asyncio.create_task(_download())
            await asyncio.sleep(0.05)
            assert len(gets) == gets_before_b, (
                "second download reached the store while the first still "
                "held the shared staging file"
            )
            may_serve.set()
            await task_a
            await task_b

        assert path.read_bytes() == content
        assert not _part_path(path).exists()
        assert not _transfer_state_path(path).exists()


class TestPublishFailureIsResumable:
    """A failed ``os.replace`` publish must leave part + checkpoint as a valid
    resume state, so the retry re-runs only the publish — never the fetch."""

    async def test_replace_failure_keeps_part_and_checkpoint(
        self, store, tmp_path
    ) -> None:
        content = bytes(range(20))
        await _put("pub/c.bin", content, store, normalize=False)
        path = tmp_path / "c.bin"

        def _download():
            return download_file_chunked(
                "pub/c.bin",
                path,
                store,
                chunk_size_bytes=10,
                max_concurrent_chunks=1,
                normalize=False,
                verify=False,
                resume=True,
            )

        part = _part_path(path)
        state = _transfer_state_path(path)
        real_replace = os.replace

        def failing_publish(src, dst, *args, **kwargs):
            if Path(src) == part:
                raise OSError("transient rename failure")
            return real_replace(src, dst, *args, **kwargs)

        with patch("application_sdk.storage.chunked.os.replace", new=failing_publish):
            with pytest.raises(StorageError, match="Failed to publish"):
                await _download()
        assert not path.exists()
        assert part.exists() and part.stat().st_size == len(content)
        assert state.exists()

        real_get = obstore.get_async
        range_gets: list[object] = []

        async def counting_get(st, key, **kw):
            if (kw.get("options") or {}).get("range"):
                range_gets.append(kw["options"]["range"])
            return await real_get(st, key, **kw)

        with patch("application_sdk.storage.ops.obstore.get_async", new=counting_get):
            await _download()

        assert range_gets == [], "retry re-fetched chunks a valid checkpoint covers"
        assert path.read_bytes() == content
        assert not part.exists()
        assert not state.exists()

    async def test_checkpoint_unlink_failure_does_not_fail_published_download(
        self, store, tmp_path
    ) -> None:
        """Once the publish has succeeded, a failed checkpoint unlink (EPERM,
        EROFS) must not report the download as failed — the stale checkpoint
        is discarded by validation on the next attempt because the part file
        is gone."""
        content = bytes(range(20))
        await _put("pub/u.bin", content, store, normalize=False)
        path = tmp_path / "u.bin"
        state = _transfer_state_path(path)

        def _download():
            return download_file_chunked(
                "pub/u.bin",
                path,
                store,
                chunk_size_bytes=10,
                max_concurrent_chunks=1,
                normalize=False,
                verify=False,
                resume=True,
            )

        real_unlink = Path.unlink

        def deny_state_unlink(self, missing_ok=False):
            if self == state:
                raise PermissionError(13, "Operation not permitted")
            return real_unlink(self, missing_ok=missing_ok)

        with patch.object(Path, "unlink", deny_state_unlink):
            await _download()

        assert path.read_bytes() == content
        assert state.exists()

        await _download()
        assert path.read_bytes() == content
        assert not state.exists()


class TestStagingFilesInvisibleToWalks:
    """Both in-flight files live in ``.sdk-partial/``, so no SDK tree walk —
    a directory ``FileReference``, a prefix upload — can adopt them."""

    def test_checkpoint_and_part_are_not_listed(self, tmp_path) -> None:
        artifact = tmp_path / "records.json"
        artifact.write_bytes(b'{"row": 1}\n')
        part = _part_path(artifact)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"half a generation")
        _transfer_state_path(artifact).write_bytes(b"{}")

        from application_sdk.common._listing import safe_list_directory

        assert safe_list_directory(tmp_path) == [artifact]


class TestOwnedTempCleanupRemovesBothStagingFiles:
    """A ``mkstemp`` destination can never be resumed, so nothing later will
    ever claim its staging files — the caller has to remove them itself.

    The part file is the one that survives ``resume=False``: a chunk failure
    deletes it (``_handle_chunk_failure``), but a *publish* failure leaves it
    on disk deliberately, as a valid resume state. With a fresh temp name per
    call that state is unreachable, so it strands under ``/tmp`` until the pod
    restarts. Both cleanup sites in ``transfer.py`` defer to the writer's own
    ``_discard_transfer_state``, so a future move of the staging layout cannot
    leave them silently pointing at nothing — which is what happened when the
    checkpoint moved into ``.sdk-partial/``.
    """

    async def test_copy_leaves_no_staging_residue_when_publish_fails(
        self, store, tmp_path
    ) -> None:
        from application_sdk.storage.transfer import _upload_from_store

        content = bytes(range(256)) * 4
        await _put("copy/src.bin", content, store, normalize=False)
        target = create_memory_store()

        staged: list[Path] = []
        real_replace = os.replace

        def failing_publish(src, dst, *args, **kwargs):
            staged.append(Path(src))
            # `resume=False` means the transfer writes no checkpoint of its
            # own, so without this the checkpoint half of the cleanup has
            # nothing to remove and a regression that drops it would pass.
            # A stale sidecar at a reused temp name is the shape being pinned.
            _transfer_state_path(Path(dst)).write_bytes(b"{}")
            raise OSError("transient rename failure")

        # _upload_from_store passes no chunk_size_bytes, so a small object would
        # take the single-stream delegate — whose own `finally` cleans up, and
        # which never creates a `.part` at all. Shrink the keyword default so
        # the object under test actually goes down the chunked path.
        with (
            patch.dict(download_file_chunked.__kwdefaults__, {"chunk_size_bytes": 64}),
            patch("application_sdk.storage.chunked.os.replace", new=failing_publish),
        ):
            with pytest.raises(StorageError):
                await _upload_from_store(store, "copy/src.bin", target, "copy/dst.bin")

        assert staged, "the publish never reached os.replace — test proves nothing"
        part = staged[0]
        assert part.parent.name == PARTIAL_DIRNAME, (
            f"expected the chunked staging file, got {part} — the small-file "
            "delegate ran instead and this test is not exercising the leak"
        )
        assert not part.exists(), (
            f"{part} survived the failed copy; with a fresh mkstemp name per "
            "call no later attempt can ever claim it, so it strands until the "
            "pod restarts"
        )
        # Same destination the failing publish seeded the sidecar for: the
        # `.part` name minus its suffix.
        dest = part.parent.parent / part.name[: -len(".part")]
        assert not _transfer_state_path(dest).exists(), (
            "the checkpoint half of the cleanup was skipped — a stale sidecar "
            "at a temp name strands exactly like the part file does"
        )
        assert real_replace is os.replace  # patch scoped, not leaked

    async def test_cleanup_paths_defer_to_the_writers_discard(self) -> None:
        """Both cleanup sites must call ``_discard_transfer_state``.

        Pinning the call, not just the effect. The original spelling was
        ``Path(str(tmp) + ".transfer-state")``, which kept passing every
        behavioural test after the checkpoint moved into ``.sdk-partial/`` —
        it simply unlinked a path that could no longer exist. Re-spelling the
        two deletions from the helpers fixed that instance without removing
        the class: a third site would drift the same way on the next move.
        Calling the function the writer itself runs on 412/404 does.
        """
        import inspect

        from application_sdk.storage import reference, transfer

        for func in (
            transfer._upload_from_store,
            transfer.download,
            reference._materialize_single_file,
        ):
            source = inspect.getsource(func)
            where = f"{func.__module__}.{func.__name__}"
            assert '+ ".transfer-state"' not in source, (
                f"{where} rebuilds the checkpoint name by string concatenation; "
                "call _discard_transfer_state so the cleanup cannot drift from "
                "the writer"
            )
            assert "_discard_transfer_state(" in source, (
                f"{where} cleans up an owned temp destination without the "
                "writer's own discard, so it can silently stop matching the "
                "staging layout"
            )
