"""Cancellation must not race an orphaned worker (FND-315, FND-317).

ADR-0010 moved the parquet reader's decode and the writer's convert-and-write
into worker threads. A thread cannot be killed, so cancelling the activity
unwinds the event loop while the worker is still touching the resource it was
handed — and the lifecycle around both offloads was never re-examined against
that.

Three shapes of the same race live here:

* the reader closing a handle out from under a live decode (FND-315);
* two attempts sharing a consolidation *temp* directory (FND-315);
* two attempts sharing the *output* directory and its chunk filenames
  (FND-317) — where the orphan's late write replaced the retry's file, and the
  retry's ``FileReference`` adopted whatever else the orphan had left behind.

Every test cancels *mid-offload*, with the blocking primitive held open so the
orphan is provably still running when the loop unwinds. That is the part worth
being strict about: a happy-path test passes against the broken code, so
without cancellation coverage none of these fixes is verifiable.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from application_sdk.common.atomic import PARTIAL_DIRNAME
from application_sdk.common.file_ops import SafeFileOps
from application_sdk.common.types import DataframeType
from application_sdk.storage.formats import _STAGING_ROOT_DIRNAME
from application_sdk.storage.formats.json import JsonFileWriter
from application_sdk.storage.formats.parquet import (
    ParquetFileReader,
    ParquetFileWriter,
    _ThreadConfinedParquetReader,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio

# Generous enough that a loaded CI box never trips it, short enough that a real
# hang fails the test rather than the job.
BLOCK_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.01
# The readiness poll must outlast BLOCK_TIMEOUT_SECONDS: a gated worker parks for
# up to that long, so a shorter budget can fail the readiness assert while the
# worker is still legitimately waiting on its own gate.
POLL_ATTEMPTS = 1200  # 12 s > BLOCK_TIMEOUT_SECONDS

WORKER_THREAD_PREFIX = "sdk-blocking-"

#: Captured before any patching so :class:`GatedParquetFile` can delegate to the
#: real reader while ``pq.ParquetFile`` itself is patched out.
REAL_PARQUET_FILE = pq.ParquetFile


def _published_path(where: object) -> str:
    """Map a path handed to ``pq.write_table`` to the artifact it publishes to.

    Writers stage each chunk as ``<dir>/.sdk-partial/<name>.<token>`` and rename
    it onto ``<dir>/<name>`` once the write succeeds (FND-318). A path that is
    not staged is returned unchanged.
    """
    staged = Path(str(where))
    if staged.parent.name != PARTIAL_DIRNAME:
        return str(staged)
    return str(staged.parent.parent / staged.name.rsplit(".", 1)[0])


async def wait_until(predicate) -> bool:
    """Poll *predicate* from the event loop until it holds, or give up."""
    for _ in range(POLL_ATTEMPTS):
        if predicate():
            return True
        await asyncio.sleep(POLL_SECONDS)
    return False


def write_parquet(path: Path, rows: int) -> str:
    """Write a small single-file parquet fixture and return its path."""
    table = pa.table({"id": list(range(rows)), "value": [f"v{i}" for i in range(rows)]})
    pq.write_table(table, str(path))
    return str(path)


@dataclass(frozen=True)
class CloseRecord:
    """One observed ``ParquetFile.close()``."""

    #: Was a decode in flight on the handle at the moment it was closed?
    decoding_in_flight: bool
    #: Which thread issued the close.
    thread_name: str


class GatedParquetFile:
    """A ``ParquetFile`` whose batch iterator can be held inside ``next()``.

    Holding the iterator open is what makes the race observable: while
    ``decoding`` is true there is a live worker inside pyarrow, and any close
    that lands is a close racing a decode.
    """

    def __init__(
        self,
        file_path: str,
        *,
        batch_started: threading.Event,
        may_finish: threading.Event,
        closes: list[CloseRecord],
    ) -> None:
        self._real = REAL_PARQUET_FILE(file_path)
        self._batch_started = batch_started
        self._may_finish = may_finish
        self._closes = closes
        self.decoding = False

    def iter_batches(self, batch_size: int) -> Iterator[pa.RecordBatch]:
        for batch in self._real.iter_batches(batch_size=batch_size):
            self.decoding = True
            self._batch_started.set()
            self._may_finish.wait(timeout=BLOCK_TIMEOUT_SECONDS)
            self.decoding = False
            yield batch

    def close(self) -> None:
        self._closes.append(
            CloseRecord(
                decoding_in_flight=self.decoding,
                thread_name=threading.current_thread().name,
            )
        )
        self._real.close()


@dataclass
class GatedRead:
    """A ``ParquetFileReader`` wired to a :class:`GatedParquetFile`."""

    reader: ParquetFileReader
    batch_started: threading.Event
    may_finish: threading.Event
    closes: list[CloseRecord]


def gated_reader(tmp_path: Path, *, rows: int = 12, chunk_size: int = 4) -> GatedRead:
    """Build a reader whose every batch is gated by a released-on-demand event."""
    parquet_file = write_parquet(tmp_path / "data.parquet", rows)
    batch_started = threading.Event()
    may_finish = threading.Event()
    closes: list[CloseRecord] = []

    def open_gated(file_path: str) -> GatedParquetFile:
        return GatedParquetFile(
            file_path,
            batch_started=batch_started,
            may_finish=may_finish,
            closes=closes,
        )

    async def only_the_fixture(path, file_extension, file_names=None):
        return [parquet_file]

    # Applied for the whole test: the reader opens the handle inside the worker
    # thread, so the patch has to still be in place well after this call.
    patcher_download = patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=only_the_fixture,
    )
    patcher_open = patch.object(pq, "ParquetFile", side_effect=open_gated)
    patcher_download.start()
    patcher_open.start()

    return GatedRead(
        reader=ParquetFileReader(
            path=str(tmp_path),
            chunk_size=chunk_size,
            dataframe_type=DataframeType.pandas,
        ),
        batch_started=batch_started,
        may_finish=may_finish,
        closes=closes,
    )


@pytest.fixture(autouse=True)
def _stop_patches():
    yield
    patch.stopall()


class TestReaderCancellation:
    """``ParquetFileReader._get_batched_dataframe`` — close vs. a live worker."""

    async def test_cancelling_mid_decode_does_not_close_under_the_worker(
        self, tmp_path: Path
    ) -> None:
        """The headline race.

        Before FND-315 the generator's ``finally`` called ``pf.close()`` inline,
        so a cancellation landing at the offload ``await`` closed the handle
        while the orphaned worker was still inside ``next(batches)``, decoding
        from it.

        That the cancellation is *observed* here — with the decode still parked
        — is also the assertion that the new close never blocks the event loop:
        it waits for the decode on a worker thread, not on the loop.
        """
        gated = gated_reader(tmp_path)

        async def consume() -> None:
            async for _ in gated.reader.read_batches():
                pass

        task = asyncio.create_task(consume())
        try:
            assert await wait_until(gated.batch_started.is_set), "decode never started"

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # The worker is provably still inside the decode here — nothing has
            # released `may_finish`. Any close recorded at this point is the race.
            assert gated.closes == [], (
                "the handle was closed while a worker thread was still decoding "
                f"from it: {gated.closes}"
            )
        finally:
            # Let the orphan unwind rather than leaving it parked on the gate,
            # even if a readiness assertion failed above.
            gated.may_finish.set()

    async def test_the_close_waits_for_the_orphaned_decode_then_runs(
        self, tmp_path: Path
    ) -> None:
        """Not closing during the race must not mean never closing.

        The close is submitted to the offload pool from the ``finally``, where it
        blocks on the reader's mutex until the orphan leaves the decode — so it
        happens exactly once, off the event loop, with nothing in flight.
        """
        gated = gated_reader(tmp_path)

        async def consume() -> None:
            async for _ in gated.reader.read_batches():
                pass

        task = asyncio.create_task(consume())
        try:
            assert await wait_until(gated.batch_started.is_set), "decode never started"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            gated.may_finish.set()
        assert await wait_until(
            lambda: bool(gated.closes)
        ), "the handle the orphaned worker was left holding was never closed"

        assert len(gated.closes) == 1, f"closed more than once: {gated.closes}"
        assert gated.closes[0].decoding_in_flight is False
        assert gated.closes[0].thread_name.startswith(
            WORKER_THREAD_PREFIX
        ), "the close must run on a worker thread, never on the event loop"

    async def test_draining_a_file_closes_it_on_a_worker(self, tmp_path: Path) -> None:
        """The happy path still closes deterministically, and still off-loop."""
        gated = gated_reader(tmp_path, rows=12, chunk_size=4)
        gated.may_finish.set()

        frames = [frame async for frame in gated.reader.read_batches()]

        assert sum(len(frame) for frame in frames) == 12
        assert await wait_until(lambda: bool(gated.closes)), "the handle was not closed"
        assert len(gated.closes) == 1, f"expected exactly one close: {gated.closes}"
        assert gated.closes[0].decoding_in_flight is False
        assert gated.closes[0].thread_name.startswith(WORKER_THREAD_PREFIX)

    async def test_closing_the_generator_early_closes_the_handle(
        self, tmp_path: Path
    ) -> None:
        """A consumer that stops early still gets exactly one close."""
        gated = gated_reader(tmp_path, rows=12, chunk_size=4)
        gated.may_finish.set()

        batches = gated.reader.read_batches()
        first = await batches.__anext__()
        assert len(first) == 4
        assert gated.closes == [], "closed while frames were still being served"

        await batches.aclose()

        assert await wait_until(lambda: bool(gated.closes)), "the handle was not closed"
        assert len(gated.closes) == 1, f"expected exactly one close: {gated.closes}"
        assert gated.closes[0].decoding_in_flight is False

    async def test_a_step_queued_behind_the_close_opens_nothing(
        self, tmp_path: Path
    ) -> None:
        """The other half of the ordering guarantee.

        A cancelled caller submits its close while a decode step may still be
        *queued* rather than running. If that step then opened the file it would
        leak a handle nobody is left to close, so a closed reader must decline to
        open one.
        """
        parquet_file = write_parquet(tmp_path / "data.parquet", 4)
        reader = _ThreadConfinedParquetReader(parquet_file, chunk_size=2)

        reader.close()

        with patch.object(pq, "ParquetFile") as never_opened:
            assert reader.next_frame() is None
        never_opened.assert_not_called()


def consolidated_frame(writer: ParquetFileWriter) -> pd.DataFrame:
    """Read back everything the writer consolidated, before ``close()`` publishes.

    Consolidated files carry their published names from the moment they are
    written, but live in the writer's private staging tree until ``close()``
    moves them into ``writer.path`` (FND-317). These tests stop short of
    ``close()``, so they read the staged tree.
    """
    staged = writer._write_root
    files = sorted(
        os.path.join(staged, name)
        for name in os.listdir(staged)
        if name.endswith(".parquet")
    )
    assert files, "the writer produced no consolidated output"
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def make_writer(path: Path) -> ParquetFileWriter:
    """Build a consolidating writer that shares its output path with its peers.

    ``typename`` is what a real activity passes, and it is what makes two
    attempts land on the same output directory: the anonymous ``defer_uploads``
    path would give each writer its own ``_parquet_*`` subdirectory and hide the
    race this module is about.
    """
    return ParquetFileWriter(
        path=str(path),
        typename="test_type",
        chunk_size=500,
        buffer_size=100,
        use_consolidation=True,
        defer_uploads=True,
    )


class TestWriterAttemptIsolation:
    """``ParquetFileWriter`` consolidation temp folders — one tree per attempt."""

    async def test_two_writers_never_share_an_accumulation_directory(
        self, tmp_path: Path
    ) -> None:
        """The property the fix rests on, asserted directly.

        ``temp_folder_index`` restarts at 0 for every writer, so before FND-315
        two attempts against the same output path resolved to the identical
        directory.
        """
        first, second = make_writer(tmp_path), make_writer(tmp_path)

        assert first._get_temp_folder_path(0) != second._get_temp_folder_path(0)
        assert first._get_temp_base_path() != second._get_temp_base_path()
        # Still one shared staging parent, so cleanup and inspection stay
        # predictable — the writer-scoped token now sits at the staging root
        # rather than inside the accumulation tree (FND-317).
        assert os.path.dirname(first._ensure_staging_root()) == os.path.dirname(
            second._ensure_staging_root()
        )
        # And that parent is outside the output directory, so nothing a
        # cancelled attempt leaves behind is inside the retry's FileReference.
        assert not first._get_temp_base_path().startswith(first.path + os.sep)

    async def test_cleanup_cannot_delete_another_attempts_live_directory(
        self, tmp_path: Path
    ) -> None:
        """``_cleanup_temp_folders`` only ever removes its own writer's tree.

        The old cleanup did ``rmtree(..., ignore_errors=True)`` on a shared path,
        so a retry could delete the directory an orphaned writer was mid-write
        into and report nothing.
        """
        orphan, retry = make_writer(tmp_path), make_writer(tmp_path)
        orphan._start_new_temp_folder()
        assert orphan.current_temp_folder_path is not None
        live_file = os.path.join(orphan.current_temp_folder_path, "chunk-0.parquet")
        Path(live_file).write_bytes(b"in flight")

        retry._start_new_temp_folder()
        await retry._cleanup_temp_folders()

        assert os.path.exists(
            live_file
        ), "a retry's cleanup deleted a directory another attempt was writing into"
        assert not os.path.exists(retry._get_temp_base_path())

    async def test_an_orphaned_writer_cannot_corrupt_the_retrys_consolidation(
        self, tmp_path: Path
    ) -> None:
        """The full race, cancelled mid-offload.

        The orphan and the retry both resolve their first chunk file *before*
        either writes, so on a shared directory both name it ``chunk-0`` and the
        orphan's late write silently replaces the retry's — wrong data, no error.
        Here each writes into its own tree, so the retry consolidates exactly its
        own rows.
        """
        orphan, retry = make_writer(tmp_path), make_writer(tmp_path)

        orphan_rows = pd.DataFrame({"id": [100, 101], "value": ["orphan", "orphan"]})
        retry_rows = pd.DataFrame({"id": [0, 1], "value": ["retry", "retry"]})

        write_started = threading.Event()
        may_write = threading.Event()
        orphan_writes_done: list[str] = []
        real_write_table = pq.write_table
        # Resolved before either writer runs, so it is the path the orphan is
        # gated on — and, before the fix, the path the retry writes too.
        orphan_chunk = os.path.join(orphan._get_temp_folder_path(0), "chunk-0.parquet")

        def gated_write_table(table, where, **kwargs):
            # Gate only the orphan's own write; the retry must run at full speed
            # so it provably reaches consolidation first. Compared on the
            # published path because the writer hands `pq.write_table` a staged
            # one (FND-318) — the chunk this test is about is still `chunk-0`,
            # it just does not carry that name until the write succeeds.
            gated = (
                _published_path(where) == orphan_chunk and not write_started.is_set()
            )
            if gated:
                write_started.set()
                may_write.wait(timeout=BLOCK_TIMEOUT_SECONDS)
            result = real_write_table(table, where, **kwargs)
            if gated:
                orphan_writes_done.append(_published_path(where))
            return result

        with patch.object(pq, "write_table", side_effect=gated_write_table):
            task = asyncio.create_task(orphan._accumulate_dataframe(orphan_rows))
            try:
                assert await wait_until(
                    write_started.is_set
                ), "orphan never started writing"

                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                # The retry runs while the orphan's worker is still parked inside
                # pq.write_table, holding a chunk path it resolved before cancel.
                await retry._accumulate_dataframe(retry_rows)
            finally:
                # Release the orphan even on a failed readiness assert, so its
                # worker is never left parked on the gate.
                may_write.set()

            # Wait for the orphan's write to *land*, not merely for the path to
            # exist: on a shared directory the file is already there, written by
            # the retry, and the whole failure is the orphan replacing it
            # afterwards. Both halves are required — `orphan_writes_done` proves
            # it was the orphan's own write that ran, and the path check proves
            # the staged chunk was published (FND-318 publishes after the write
            # returns, so the two are not simultaneous).
            assert await wait_until(
                lambda: bool(orphan_writes_done) and os.path.exists(orphan_chunk)
            ), "the orphaned worker never landed its chunk"

            await retry._consolidate_current_folder()

        written = consolidated_frame(retry)
        assert sorted(written["id"].tolist()) == [0, 1]
        assert set(written["value"]) == {
            "retry"
        }, "the orphaned writer's rows reached the retry's consolidated output"

        # And the orphan's own write landed in its own tree rather than being
        # deleted or redirected.
        assert set(pd.read_parquet(orphan_chunk)["value"]) == {"orphan"}


def make_output_writer(path: Path, **overrides: object) -> ParquetFileWriter:
    """Build a non-consolidating writer that shares its output path with peers.

    The consolidation-free path is the one that reaches ``Writer._flush_buffer``
    and therefore names its files with the base class's ``path_gen`` — the
    attempt-independent name every writer subclass derives (FND-317).
    """
    kwargs: dict[str, object] = {
        "path": str(path),
        "typename": "test_type",
        "chunk_size": 500,
        "buffer_size": 100,
        "defer_uploads": True,
    }
    kwargs.update(overrides)
    return ParquetFileWriter(**kwargs)  # type: ignore[arg-type]


def files_under(directory: str) -> list[str]:
    """Every regular file under *directory*, as sorted relative paths."""
    return sorted(
        str(p.relative_to(directory)) for p in Path(directory).rglob("*") if p.is_file()
    )


class TestWriterOutputIsolation:
    """``Writer`` output directory — one attempt's files, published at close."""

    async def test_an_orphaned_writer_cannot_reach_the_retrys_published_output(
        self, tmp_path: Path
    ) -> None:
        """The headline race, cancelled mid-offload.

        Both attempts resolve ``chunk-0-part0.parquet`` under the same
        ``typename`` directory, so before FND-317 the orphan's late write
        replaced the retry's file — and any file of the orphan's that did *not*
        collide was uploaded as part of the retry's output, because
        ``_build_file_reference`` listed the directory rather than the writer.

        The orphan is released only after the retry has published, so its write
        provably lands second: exactly the ordering that used to win.
        """
        orphan, retry = make_output_writer(tmp_path), make_output_writer(tmp_path)
        assert orphan.path == retry.path, "the attempts must share an output directory"

        orphan_rows = pd.DataFrame({"id": [100, 101], "value": ["orphan", "orphan"]})
        retry_rows = pd.DataFrame({"id": [0, 1], "value": ["retry", "retry"]})

        write_started = threading.Event()
        may_write = threading.Event()
        orphan_writes_done: list[str] = []
        real_write_table = pq.write_table
        # Resolved before either writer runs, so it is the path the orphan is
        # gated on — and, before the fix, the path the retry writes too.
        orphan_chunk = os.path.join(orphan._write_root, "chunk-0-part0.parquet")

        def gated_write_table(table, where, **kwargs):
            # Compared on the published path: each chunk is also staged
            # individually inside the writer's staging tree (FND-318), so the
            # path pq.write_table is handed is not the one this test names.
            gated = (
                _published_path(where) == orphan_chunk and not write_started.is_set()
            )
            if gated:
                write_started.set()
                may_write.wait(timeout=BLOCK_TIMEOUT_SECONDS)
            result = real_write_table(table, where, **kwargs)
            if gated:
                orphan_writes_done.append(_published_path(where))
            return result

        with patch.object(pq, "write_table", side_effect=gated_write_table):
            task = asyncio.create_task(orphan.write(orphan_rows))
            try:
                assert await wait_until(
                    write_started.is_set
                ), "orphan never started writing"

                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                # The retry runs to completion — including the publish inside
                # close() — while the orphan's worker is still parked inside
                # pq.write_table, holding a path it resolved before the cancel.
                await retry.write(retry_rows)
                result = await retry.close()
            finally:
                # Release the orphan even on a failed assertion, so its worker
                # is never left parked on the gate.
                may_write.set()

            # Both halves: `orphan_writes_done` proves the orphan's own write
            # ran, and the path check proves its staged chunk was published
            # into the orphan's tree — the two are not simultaneous, since
            # FND-318 renames after the write returns.
            assert await wait_until(
                lambda: bool(orphan_writes_done) and os.path.exists(orphan_chunk)
            ), "the orphaned worker never landed its chunk"

        assert files_under(retry.path) == [
            "chunk-0-part0.parquet",
            os.path.join("statistics", "statistics.json.ignore"),
        ], "the retry's output directory holds something it did not write"

        published = pd.read_parquet(os.path.join(retry.path, "chunk-0-part0.parquet"))
        assert sorted(published["id"].tolist()) == [0, 1]
        assert set(published["value"]) == {
            "retry"
        }, "the orphaned writer's late write replaced the retry's chunk"

        # The FileReference the interceptor persists covers that directory, so
        # its file count is the mode-2 assertion stated directly.
        assert result.files is not None
        assert result.files.local_path == retry.path
        assert result.files.file_count == 2

        # The orphan's own write landed, in its own tree, outside the output.
        assert set(pd.read_parquet(orphan_chunk)["value"]) == {"orphan"}
        assert not orphan_chunk.startswith(retry.path + os.sep)

    async def test_pre_existing_files_in_the_output_directory_are_adopted(
        self, tmp_path: Path
    ) -> None:
        """Pin the contract for content already in the output directory.

        ``_publish_staged_files`` only ever *adds* this writer's staged files —
        it never removes what was already under ``self.path``. And because the
        deferred writer returns ``FileReference.from_local(self.path)``, which
        walks the directory recursively, that pre-existing content is adopted
        into the writer's returned reference and uploaded as part of its output.

        This is the same adoption mechanism this PR removes for a cancelled
        attempt's orphan files — those now live in the orphan's private staging
        tree. But the hole stays open for files that pre-date the writer, which
        staging does not (and by design cannot) clean. This test pins that
        behavior so the contract is explicit rather than accidental.
        """
        output_dir = os.path.join(str(tmp_path), "test_type")
        os.makedirs(output_dir)
        stale = os.path.join(output_dir, "stale-chunk.parquet")
        pq.write_table(pa.table({"id": [999]}), stale)

        writer = make_output_writer(tmp_path)
        await writer.write(pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]}))
        result = await writer.close()

        # The pre-existing file survived the publish (not cleaned)...
        assert "stale-chunk.parquet" in files_under(writer.path)
        # ...and was adopted into the returned FileReference: the writer's own
        # chunk + statistics sidecar + the stale file it did not write.
        assert result.files is not None
        assert result.files.file_count == 3

    async def test_two_writers_never_resolve_the_same_output_filename(
        self, tmp_path: Path
    ) -> None:
        """The property the fix rests on, asserted directly.

        ``chunk_count`` and ``chunk_part`` both restart at 0 for a fresh
        writer, so the *published* name is deliberately identical between
        attempts — it is a downstream contract. What must differ is the path
        each writer actually writes to.
        """
        first, second = make_output_writer(tmp_path), make_output_writer(tmp_path)

        first_chunk = os.path.join(first._write_root, "chunk-0-part0.parquet")
        second_chunk = os.path.join(second._write_root, "chunk-0-part0.parquet")

        assert first_chunk != second_chunk
        # ...while the published name each maps to is byte-identical, which is
        # what makes this fix safe for consumers reading `chunk-*.parquet`.
        published = os.path.join(str(tmp_path), "test_type", "chunk-0-part0.parquet")
        assert first._published_path(first_chunk) == published
        assert second._published_path(second_chunk) == published

    async def test_the_output_directory_stays_empty_until_close(
        self, tmp_path: Path
    ) -> None:
        """Nothing is published by a writer that never reaches ``close()``.

        This is the whole mechanism: a cancelled attempt cannot leave a file
        behind in the output directory because it never had one there.
        """
        writer = make_output_writer(tmp_path)
        await writer.write(pd.DataFrame({"id": [1, 2, 3]}))

        assert files_under(writer.path) == [], "a chunk reached the output before close"
        assert os.path.exists(
            os.path.join(writer._write_root, "chunk-0-part0.parquet")
        ), "the chunk was not staged"
        staging_root = writer._staging_root
        assert staging_root is not None

        await writer.close()

        assert files_under(writer.path) == [
            "chunk-0-part0.parquet",
            os.path.join("statistics", "statistics.json.ignore"),
        ]
        assert not os.path.exists(staging_root), "the staging tree outlived the publish"

    async def test_inline_uploads_use_the_published_key_not_the_staged_path(
        self, tmp_path: Path
    ) -> None:
        """Staging is invisible to the object store.

        ``defer_uploads=False`` uploads each chunk before ``close()`` publishes
        it, so the key has to be derived from where the file *will* live, not
        from where it is being read.
        """
        writer = make_output_writer(tmp_path, defer_uploads=False)
        uploads: list[tuple[str, str]] = []

        async def record_upload(key, local_path, **kwargs):
            uploads.append((str(key), str(local_path)))

        with patch(
            "application_sdk.storage.formats._upload_file", side_effect=record_upload
        ):
            await writer.write(pd.DataFrame({"id": [1, 2, 3]}))
            await writer.close()

        assert uploads, "nothing was uploaded on the inline path"
        keys = [key for key, _ in uploads]
        assert os.path.join(writer.path, "chunk-0-part0.parquet") in keys
        assert os.path.join(writer.path, "statistics", "statistics.json.ignore") in keys
        assert not any(
            _STAGING_ROOT_DIRNAME in key for key in keys
        ), f"a staged path leaked into an object-store key: {keys}"
        # ...and every upload still read the file from the staging tree.
        assert all(_STAGING_ROOT_DIRNAME in local for _, local in uploads)

    async def test_an_orphaned_json_writer_cannot_append_to_the_retrys_output(
        self, tmp_path: Path
    ) -> None:
        """The same race on the JSON writer, where it is worse.

        ``JsonFileWriter._write_chunk`` resolves its open mode — ``wb`` or
        append — before the offload, so on a shared output directory an
        orphan's late write either truncated the retry's chunk or appended to
        it, depending on which attempt saw the file first. Both are silent
        corruption of a file the retry believes it owns.
        """
        orphan = JsonFileWriter(
            path=str(tmp_path), typename="test_type", buffer_size=100
        )
        retry = JsonFileWriter(
            path=str(tmp_path), typename="test_type", buffer_size=100
        )
        assert orphan.path == retry.path

        write_started = threading.Event()
        may_write = threading.Event()
        orphan_writes_done: list[str] = []
        real_open = SafeFileOps.open
        orphan_chunk = os.path.join(orphan._write_root, "chunk-0-part0.json")

        def gated_open(file, mode="r", *args, **kwargs):
            gated = str(file) == orphan_chunk and not write_started.is_set()
            if gated:
                write_started.set()
                may_write.wait(timeout=BLOCK_TIMEOUT_SECONDS)
                orphan_writes_done.append(str(file))
            return real_open(file, mode, *args, **kwargs)

        # JsonFileWriter has no deferred-upload mode, so stub the store out —
        # this test is about what reaches the local output directory.
        with (
            patch("application_sdk.storage.formats._upload_file", new=AsyncMock()),
            patch.object(SafeFileOps, "open", side_effect=gated_open),
        ):
            task = asyncio.create_task(
                orphan.write(pd.DataFrame({"id": [100], "value": ["orphan"]}))
            )
            try:
                assert await wait_until(
                    write_started.is_set
                ), "orphan never started writing"

                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                await retry.write(pd.DataFrame({"id": [0], "value": ["retry"]}))
                await retry.close()
            finally:
                may_write.set()

            assert await wait_until(
                lambda: bool(orphan_writes_done)
            ), "the orphaned worker never landed its chunk"

        published = pd.read_json(
            os.path.join(retry.path, "chunk-0-part0.json"), lines=True
        )
        assert published["value"].tolist() == [
            "retry"
        ], "the orphaned writer's rows were appended to the retry's chunk"

    async def test_a_cross_filesystem_publish_never_writes_directly_to_the_final_name(
        self, tmp_path: Path
    ) -> None:
        """The EXDEV fallback is staged, not direct-to-final (FND-318 review).

        When the output directory is itself a mount point, staging and output
        sit on different filesystems, ``os.replace`` raises ``EXDEV``, and the
        publish must fall back to a copy. That copy used to be ``shutil.move``
        straight onto the artifact's real name — an interruption or ``ENOSPC``
        mid-copy left a truncated file at the very path staging exists to keep
        clean. The fallback now routes through ``atomic_copy`` (stage next to
        the destination, fsync, rename) and unlinks the source itself, so the
        final name only ever appears complete even on that layout.
        """
        import errno as errno_module

        writer = make_output_writer(tmp_path)
        await writer.write(pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]}))

        real_replace = os.replace
        moved: list[tuple[str, str]] = []

        # Refuse only renames that land in the output directory — staging tree
        # → ``writer.path`` — with EXDEV, as a mount-point output directory
        # would. Renames that stay inside the staging tree (atomic_write's own
        # publish of the statistics sidecar) or stage next to their
        # destination inside the output directory (atomic_copy's publish) are
        # same-filesystem by construction and must pass through, or the fake
        # would simulate a filesystem that cannot rename within one directory
        # — a different failure entirely.
        output_dir = os.path.normpath(writer.path)

        def refuse_cross_filesystem_rename(src, dst, **kwargs):
            if str(dst).startswith(
                output_dir + os.sep
            ) and _STAGING_ROOT_DIRNAME in str(src):
                raise OSError(errno_module.EXDEV, "Invalid cross-device link")
            return real_replace(src, dst, **kwargs)

        def no_direct_move(src, dst, **kwargs):
            moved.append((str(src), str(dst)))
            return real_replace(src, dst, **kwargs)

        with (
            patch.object(os, "replace", side_effect=refuse_cross_filesystem_rename),
            patch("shutil.move", side_effect=no_direct_move),
        ):
            await writer.close()

        assert moved == [], (
            "the cross-filesystem fallback published with shutil.move "
            f"direct-to-final: {moved}"
        )
        # The staged copy still landed the chunk under its real name.
        assert "chunk-0-part0.parquet" in files_under(writer.path)
        assert set(
            pd.read_parquet(os.path.join(writer.path, "chunk-0-part0.parquet"))["value"]
        ) == {
            "a",
            "b",
            "c",
        }
