"""Regression tests for FND-315: cancellation must not race an orphaned worker.

ADR-0010 moved the parquet reader's decode and the writer's convert-and-write
into worker threads. A thread cannot be killed, so cancelling the activity
unwinds the event loop while the worker is still touching the resource it was
handed — and the lifecycle around both offloads was never re-examined against
that.

Every test here cancels *mid-offload*, with the blocking primitive held open so
the orphan is provably still running when the loop unwinds. That is the part
worth being strict about: a happy-path test passes against the broken code, so
without cancellation coverage neither fix is verifiable.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from application_sdk.common.types import DataframeType
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
POLL_ATTEMPTS = 500

WORKER_THREAD_PREFIX = "sdk-blocking-"

#: Captured before any patching so :class:`GatedParquetFile` can delegate to the
#: real reader while ``pq.ParquetFile`` itself is patched out.
REAL_PARQUET_FILE = pq.ParquetFile


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

        # Let the orphan unwind rather than leaving it parked on the gate.
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
        assert await wait_until(gated.batch_started.is_set), "decode never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

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
    """Read back everything the writer consolidated into its output directory."""
    files = sorted(
        os.path.join(writer.path, name)
        for name in os.listdir(writer.path)
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
        # Still one shared parent, so cleanup and inspection stay predictable.
        assert os.path.dirname(first._get_temp_base_path()) == os.path.dirname(
            second._get_temp_base_path()
        )

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
            # so it provably reaches consolidation first.
            gated = str(where) == orphan_chunk and not write_started.is_set()
            if gated:
                write_started.set()
                may_write.wait(timeout=BLOCK_TIMEOUT_SECONDS)
            result = real_write_table(table, where, **kwargs)
            if gated:
                orphan_writes_done.append(str(where))
            return result

        with patch.object(pq, "write_table", side_effect=gated_write_table):
            task = asyncio.create_task(orphan._accumulate_dataframe(orphan_rows))
            assert await wait_until(
                write_started.is_set
            ), "orphan never started writing"

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # The retry runs while the orphan's worker is still parked inside
            # pq.write_table, holding a chunk path it resolved before cancel.
            await retry._accumulate_dataframe(retry_rows)

            # Release the orphan and wait for its write to *land*, not merely for
            # the path to exist: on a shared directory the file is already there,
            # written by the retry, and the whole failure is the orphan replacing
            # it afterwards.
            may_write.set()
            assert await wait_until(
                lambda: bool(orphan_writes_done)
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
