"""Framework ``mark_progress()`` hooks on the batched output writers (FND-288).

ADR-0018's hard constraint for these hooks is *batch and chunk boundaries,
never per record* — so every test here asserts a **count**, not just presence.
A hook that fired per record would still make the presence assertions pass.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from application_sdk.storage.formats.json import JsonFileWriter
from application_sdk.storage.formats.parquet import ParquetFileWriter
from application_sdk.storage.rolling import CountPolicy, RollingFileWriter
from tests.unit.conftest import RecordingProgressTracker

_ROWS = 1000
_BUFFER = 100


def _frame(rows: int = _ROWS) -> pd.DataFrame:
    return pd.DataFrame({"id": range(rows), "name": [f"n{i}" for i in range(rows)]})


# ---------------------------------------------------------------------------
# Writer._flush_buffer — the boundary every writer subclass shares
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_buffer_marks_once_per_chunk_never_per_record(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    writer = ParquetFileWriter(
        path=str(tmp_path / "out"), buffer_size=_BUFFER, defer_uploads=True
    )
    await writer.write(_frame())

    assert progress_marks.count("writer.flush_buffer") == _ROWS // _BUFFER
    # The whole point of the constraint: the row count must not leak into the
    # mark count.
    assert len(progress_marks.labels) < _ROWS


@pytest.mark.asyncio
async def test_json_writer_inherits_the_same_flush_hook(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    """JsonFileWriter overrides only ``_write_chunk``, so it gets the hook free."""
    with patch("application_sdk.storage.formats._upload_file"):
        writer = JsonFileWriter(path=str(tmp_path / "json"), buffer_size=_BUFFER)
        await writer.write(_frame())

    assert progress_marks.count("writer.flush_buffer") == _ROWS // _BUFFER


@pytest.mark.asyncio
async def test_an_empty_write_marks_nothing(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    """No unit of work completed means no progress signal.

    A mark on a no-op write would forgive real quiet time — an activity looping
    over empty frames is exactly the wedged-but-looping shape the watchdog
    exists to catch.
    """
    writer = ParquetFileWriter(path=str(tmp_path / "empty"), defer_uploads=True)
    await writer.write(pd.DataFrame())

    assert progress_marks.labels == []


@pytest.mark.asyncio
async def test_batched_write_marks_every_batch(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    """``write_batches`` streams; each batch's chunks must each be marked."""

    def batches():
        for _ in range(4):
            yield _frame(_BUFFER)

    writer = ParquetFileWriter(
        path=str(tmp_path / "batched"), buffer_size=_BUFFER, defer_uploads=True
    )
    await writer.write_batches(batches())

    assert progress_marks.count("writer.flush_buffer") == 4


# ---------------------------------------------------------------------------
# close() → the statistics emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_marks_the_statistics_emission_once(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    writer = ParquetFileWriter(
        path=str(tmp_path / "stats"), buffer_size=_BUFFER, defer_uploads=True
    )
    await writer.write(_frame(_BUFFER))
    await writer.close()

    assert progress_marks.count("writer.statistics") == 1
    # It is the *last* thing the writer emits, so it must be the label an
    # operator sees if the attempt goes quiet right after a writer finished.
    assert progress_marks.labels[-1] == "writer.statistics"


@pytest.mark.asyncio
async def test_a_second_close_marks_nothing_more(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    """close() is idempotent; its cached-result path does no work to report."""
    writer = ParquetFileWriter(
        path=str(tmp_path / "twice"), buffer_size=_BUFFER, defer_uploads=True
    )
    await writer.write(_frame(_BUFFER))
    await writer.close()
    await writer.close()

    assert progress_marks.count("writer.statistics") == 1


# ---------------------------------------------------------------------------
# The parquet consolidation path, which never reaches _flush_buffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidation_path_marks_accumulation_and_consolidation(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    def batches():
        for _ in range(4):
            yield _frame(_BUFFER)

    writer = ParquetFileWriter(
        path=str(tmp_path / "consolidated"),
        buffer_size=_BUFFER,
        use_consolidation=True,
        defer_uploads=True,
    )
    writer.consolidation_threshold = 2 * _BUFFER
    await writer.write_batches(batches())

    # Four accumulated chunks, and two consolidated files (two folders hit the
    # 200-record threshold; each consolidates into one file — FND-1339 stopped
    # the consolidation loop re-slicing by buffer_size). The consolidation path
    # bypasses _flush_buffer entirely, so without its own hooks this whole
    # stream would be one quiet window.
    assert progress_marks.count("writer.accumulate_chunk") == 4
    assert progress_marks.count("writer.consolidate_chunk") == 2
    assert progress_marks.count("writer.flush_buffer") == 0


# ---------------------------------------------------------------------------
# RollingFileWriter — the recommended replacement, and not a Writer subclass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rolling_writer_marks_once_per_rolled_chunk(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    flushed: list[str] = []

    def flush_fn(batches: list[pd.DataFrame], path: str) -> None:
        pd.concat(batches, ignore_index=True).to_json(path, orient="records")
        flushed.append(path)

    async with RollingFileWriter[pd.DataFrame](
        base_path=str(tmp_path),
        extension=".json",
        flush_fn=flush_fn,
        rollover_policy=CountPolicy(max_records=_BUFFER),
    ) as writer:
        for _ in range(3):
            await writer.append(_frame(_BUFFER))

    assert progress_marks.count("writer.rolling_flush") == len(flushed)
    assert progress_marks.count("writer.rolling_flush") == 3


@pytest.mark.asyncio
async def test_rolling_writer_marks_before_the_completion_callback(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    """A slow ``on_chunk_complete`` must be measured, not excused.

    The mark lands before the callback runs, so time spent inside a caller's
    callback counts toward the no-progress window rather than being credited to
    the chunk that preceded it.
    """
    seen_at_callback: list[int] = []

    def flush_fn(batches: list[pd.DataFrame], path: str) -> None:
        pd.concat(batches, ignore_index=True).to_json(path, orient="records")

    def on_chunk_complete(index: int, path: str) -> None:
        seen_at_callback.append(progress_marks.count("writer.rolling_flush"))

    async with RollingFileWriter[pd.DataFrame](
        base_path=str(tmp_path),
        extension=".json",
        flush_fn=flush_fn,
        rollover_policy=CountPolicy(max_records=_BUFFER),
        on_chunk_complete=on_chunk_complete,
    ) as writer:
        await writer.append(_frame(_BUFFER))

    assert seen_at_callback == [1]


@pytest.mark.asyncio
async def test_a_failed_flush_marks_nothing(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    def flush_fn(batches: list[pd.DataFrame], path: str) -> None:
        raise OSError("disk full")

    writer = RollingFileWriter[pd.DataFrame](
        base_path=str(tmp_path),
        extension=".json",
        flush_fn=flush_fn,
        rollover_policy=CountPolicy(max_records=_BUFFER),
    )
    with pytest.raises(OSError, match="disk full"):
        await writer.append(_frame(_BUFFER))

    assert progress_marks.labels == []


# ---------------------------------------------------------------------------
# Inertness — the hooks must be invisible until a tracker is bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writers_behave_identically_with_no_tracker_bound(
    tmp_path: Path,
) -> None:
    """No ``progress_marks`` fixture here: no tracker is bound, on purpose.

    Outside an activity ``current_progress_tracker()`` hands back the inert
    tracker, so every hook discards its signal and behaviour is unchanged.
    """
    writer = ParquetFileWriter(
        path=str(tmp_path / "inert"), buffer_size=_BUFFER, defer_uploads=True
    )
    await writer.write(_frame())
    result = await writer.close()

    assert result.total_record_count == _ROWS
    assert os.path.isdir(str(tmp_path / "inert"))
