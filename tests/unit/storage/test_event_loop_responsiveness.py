"""Regression tests for FND-282: the write/read hot paths must not starve the loop.

Heartbeat timeouts across the fleet are dominated by event-loop starvation
rather than by timeout semantics (ADR-0018, *Problem 2*): blocking work holds
the loop, the auto-heartbeat coroutine never gets to run, and Temporal kills an
activity that was making progress the whole time.

Each test here drives a hot path while a ticker coroutine counts how often the
loop got to run something else. The blocking primitive underneath is slowed
down so a regression that puts it back on the loop stalls the ticker and fails
the assertion. This asserts the property ADR-0010 actually cares about — the
loop stays free to beat — rather than which callable was handed to
``run_in_thread``, so it survives the blocking sections being regrouped.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import orjson
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from application_sdk.storage.formats.json import JsonFileReader, JsonFileWriter
from application_sdk.storage.formats.parquet import ParquetFileReader
from application_sdk.storage.rolling import RollingFileWriter

pytestmark = pytest.mark.asyncio

T = TypeVar("T")

# How long the patched blocking primitive pretends to take. Long enough that a
# regression is unambiguous, short enough to keep the suite fast.
BLOCK_SECONDS = 0.3
TICK_SECONDS = 0.01
# A responsive loop gets ~30 ticks per blocking call; a blocked one gets 0.
# 5 leaves generous headroom for a loaded CI box.
MIN_TICKS = 5


async def count_ticks_during(work: Callable[[], Awaitable[T]]) -> tuple[T, int]:
    """Run *work*, returning its result and how many times the loop ticked."""
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK_SECONDS)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        result = await work()
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
    return result, ticks


def assert_stayed_responsive(ticks: int, what: str) -> None:
    assert ticks >= MIN_TICKS, (
        f"{what} stalled the event loop ({ticks} ticks) — a starved loop cannot "
        "run the auto-heartbeat, which is what gets healthy activities killed"
    )


def write_jsonl(path: Path, rows: int) -> Path:
    with path.open("wb") as f:
        for i in range(rows):
            f.write(orjson.dumps({"id": i, "name": f"row-{i}"}))
            f.write(b"\n")
    return path


class TestJsonWriterResponsiveness:
    async def test_write_chunk_does_not_block_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        writer = JsonFileWriter(str(tmp_path / "out"))
        chunk = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
        file_name = str(tmp_path / "out" / "chunk.json")

        real_dumps = orjson.dumps
        slowed = {"done": False}

        def _slow_dumps(*args: Any, **kwargs: Any) -> bytes:
            # Charge the cost once, standing in for a full-size chunk.
            if not slowed["done"]:
                slowed["done"] = True
                time.sleep(BLOCK_SECONDS)
            return real_dumps(*args, **kwargs)

        monkeypatch.setattr(
            "application_sdk.storage.formats.json.orjson.dumps", _slow_dumps
        )

        _, ticks = await count_ticks_during(
            lambda: writer._write_chunk(chunk, file_name)
        )

        assert slowed["done"], "the serialise path never ran"
        assert_stayed_responsive(ticks, "JsonFileWriter._write_chunk")
        assert Path(file_name).read_bytes().count(b"\n") == 3


class TestJsonReaderResponsiveness:
    async def test_batched_read_does_not_block_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_jsonl(tmp_path / "records.json", rows=20)
        reader = JsonFileReader(str(tmp_path), chunk_size=10)

        real_loads = orjson.loads
        calls = {"n": 0}

        def _slow_loads(*args: Any, **kwargs: Any) -> Any:
            # Charge the cost on the first record of each batch.
            if calls["n"] % 10 == 0:
                time.sleep(BLOCK_SECONDS / 2)
            calls["n"] += 1
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(
            "application_sdk.storage.formats.json.orjson.loads", _slow_loads
        )

        async def _drain() -> list[pd.DataFrame]:
            return [frame async for frame in reader._get_batched_dataframe()]

        frames, ticks = await count_ticks_during(_drain)

        assert [len(f) for f in frames] == [10, 10]
        assert calls["n"] == 20, "the patched orjson.loads path never ran"
        assert_stayed_responsive(ticks, "JsonFileReader._get_batched_dataframe")

    async def test_whole_file_read_does_not_block_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_jsonl(tmp_path / "records.json", rows=5)
        reader = JsonFileReader(str(tmp_path))

        real_loads = orjson.loads
        slowed = {"done": False}

        def _slow_loads(*args: Any, **kwargs: Any) -> Any:
            if not slowed["done"]:
                slowed["done"] = True
                time.sleep(BLOCK_SECONDS)
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(
            "application_sdk.storage.formats.json.orjson.loads", _slow_loads
        )

        frame, ticks = await count_ticks_during(reader._get_dataframe)

        assert len(frame) == 5
        assert slowed["done"], "the read path never ran"
        assert_stayed_responsive(ticks, "JsonFileReader._get_dataframe")


class TestParquetReaderResponsiveness:
    async def test_whole_prefix_read_does_not_block_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = pa.Table.from_pandas(
            pd.DataFrame({"id": [1, 2, 3]}), preserve_index=False
        )
        pq.write_table(table, str(tmp_path / "chunk-0.parquet"))
        reader = ParquetFileReader(str(tmp_path))

        real_read_table = pq.read_table
        slowed = {"done": False}

        def _slow_read_table(*args: Any, **kwargs: Any) -> pa.Table:
            if not slowed["done"]:
                slowed["done"] = True
                time.sleep(BLOCK_SECONDS)
            return real_read_table(*args, **kwargs)

        monkeypatch.setattr("pyarrow.parquet.read_table", _slow_read_table)

        frame, ticks = await count_ticks_during(reader._get_dataframe)

        assert slowed["done"], "the read path never ran"
        assert len(frame) == 3
        assert_stayed_responsive(ticks, "ParquetFileReader._get_dataframe")

    async def test_batched_read_does_not_block_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = pa.Table.from_pandas(
            pd.DataFrame({"id": list(range(20))}), preserve_index=False
        )
        pq.write_table(table, str(tmp_path / "chunk-0.parquet"), row_group_size=5)
        reader = ParquetFileReader(str(tmp_path), chunk_size=5)

        real_parquet_file = pq.ParquetFile
        slowed = {"done": False}

        def _slow_parquet_file(*args: Any, **kwargs: Any) -> Any:
            if not slowed["done"]:
                slowed["done"] = True
                time.sleep(BLOCK_SECONDS)
            return real_parquet_file(*args, **kwargs)

        monkeypatch.setattr("pyarrow.parquet.ParquetFile", _slow_parquet_file)

        async def _drain() -> list[pd.DataFrame]:
            return [frame async for frame in reader._get_batched_dataframe()]

        frames, ticks = await count_ticks_during(_drain)

        assert slowed["done"], "the read path never ran"
        assert sum(len(f) for f in frames) == 20
        assert_stayed_responsive(ticks, "ParquetFileReader._get_batched_dataframe")


class TestRollingWriterResponsiveness:
    """The recommended (non-deprecated) writer runs a caller-supplied flush_fn.

    Its own documented parquet example is a plain function doing
    ``pd.concat(...).to_parquet(...)``, so if the writer calls a sync flush_fn
    inline, every connector following the docs blocks the loop on every chunk.
    """

    async def test_sync_flush_fn_runs_off_the_loop(self, tmp_path: Path) -> None:
        flushed: list[tuple[int, str]] = []

        def _blocking_flush(batches: list[pd.DataFrame], path: str) -> None:
            time.sleep(BLOCK_SECONDS)
            pd.concat(batches, ignore_index=True).to_parquet(path)
            flushed.append((len(batches), path))

        async def _run() -> None:
            async with RollingFileWriter[pd.DataFrame](
                base_path=str(tmp_path / "rolling"),
                extension=".parquet",
                flush_fn=_blocking_flush,
            ) as writer:
                await writer.append(pd.DataFrame({"id": [1, 2]}))
                await writer.flush()

        _, ticks = await count_ticks_during(_run)

        assert len(flushed) == 1, "flush_fn did not run"
        assert_stayed_responsive(ticks, "RollingFileWriter sync flush_fn")

    async def test_async_flush_fn_is_still_awaited_directly(
        self, tmp_path: Path
    ) -> None:
        """An async flush_fn yields on its own; it must not be sent to a thread."""
        seen: list[str] = []

        async def _async_flush(batches: list[pd.DataFrame], path: str) -> None:
            await asyncio.sleep(0)
            pd.concat(batches, ignore_index=True).to_parquet(path)
            seen.append(path)

        async with RollingFileWriter[pd.DataFrame](
            base_path=str(tmp_path / "rolling-async"),
            extension=".parquet",
            flush_fn=_async_flush,
        ) as writer:
            await writer.append(pd.DataFrame({"id": [1, 2]}))
            await writer.flush()

        assert len(seen) == 1
        assert Path(seen[0]).exists()

    async def test_sync_on_chunk_complete_runs_off_the_loop(
        self, tmp_path: Path
    ) -> None:
        completed: list[int] = []

        def _flush(batches: list[pd.DataFrame], path: str) -> None:
            pd.concat(batches, ignore_index=True).to_parquet(path)

        def _blocking_callback(index: int, path: str) -> None:
            time.sleep(BLOCK_SECONDS)
            completed.append(index)

        async def _run() -> None:
            async with RollingFileWriter[pd.DataFrame](
                base_path=str(tmp_path / "rolling-cb"),
                extension=".parquet",
                flush_fn=_flush,
                on_chunk_complete=_blocking_callback,
            ) as writer:
                await writer.append(pd.DataFrame({"id": [1, 2]}))
                await writer.flush()

        _, ticks = await count_ticks_during(_run)

        assert completed == [0]
        assert_stayed_responsive(ticks, "RollingFileWriter sync on_chunk_complete")
