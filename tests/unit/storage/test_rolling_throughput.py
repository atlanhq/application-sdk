"""Throughput guard: RollingFileWriter vs the writers it replaces.

``RollingFileWriter`` is the migration target for the v4.0-deprecated
``ParquetFileWriter`` / ``JsonFileWriter``, so migrating must never make an app
slower. Each test writes the same 200k rows (20 batches of 10k) through the
deprecated writer, including its per-slice ``gc.collect()``, and through
``RollingFileWriter`` in the shape the migration guide documents — DataFrame
batches for Parquet, ``list[dict]`` + orjson for JSON — and asserts the
replacement is no slower.

Uploads are excluded on both sides: ``RollingFileWriter`` leaves them to the
activity interceptor, so the deprecated writers stage locally too.

The comparison is paired, in one process, rather than an absolute time: GC
cost scales with the heap, so the deprecated side's figure depends on what the
process has imported (≈0.8 s on a laptop running this module, ≈12 s in the
production-heap microbenchmark). The replacement side takes the best of a few
runs, so a noisy runner can only make the guard more lenient, never fail it
spuriously. Measured figures are recorded in
``docs/agents/coding-standards.md``.

Marked ``perf``: at ~35 s per format on a CI runner it ran on every matrix leg,
timed beside xdist neighbours on the same cores. It now runs once per CI run in
the single-process ``Unit Perf Tests`` job.
"""

from __future__ import annotations

import time
import warnings
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pd = pytest.importorskip("pandas")
orjson = pytest.importorskip("orjson")

from application_sdk.storage.formats.json import JsonFileWriter  # noqa: E402
from application_sdk.storage.formats.parquet import ParquetFileWriter  # noqa: E402
from application_sdk.storage.rolling import RollingFileWriter  # noqa: E402

ROWS = 200_000
BATCH_ROWS = 10_000
ROLLING_RUNS = 3

pytestmark = pytest.mark.perf


def _frames() -> Iterator[pd.DataFrame]:
    for start in range(0, ROWS, BATCH_ROWS):
        ids = range(start, start + BATCH_ROWS)
        yield pd.DataFrame(
            {
                "id": ids,
                "name": [f"col_{i}" for i in ids],
                "type": ["varchar"] * BATCH_ROWS,
                "pos": ids,
            }
        )


def _row_batches() -> Iterator[list[dict[str, Any]]]:
    for start in range(0, ROWS, BATCH_ROWS):
        yield [
            {"id": i, "name": f"col_{i}", "type": "varchar", "pos": i}
            for i in range(start, start + BATCH_ROWS)
        ]


def _flush_parquet(batches: list[pd.DataFrame], path: str) -> None:
    pd.concat(batches, ignore_index=True).to_parquet(path)


def _flush_jsonl(batches: list[list[dict[str, Any]]], path: str) -> None:
    with open(path, "wb") as fh:
        for batch in batches:
            for record in batch:
                fh.write(orjson.dumps(record))
                fh.write(b"\n")


async def _timed(fn: Callable[[], Awaitable[None]]) -> float:
    started = time.perf_counter()
    await fn()
    return time.perf_counter() - started


async def _no_upload(_file_name: str) -> None:
    return None


async def _deprecated_parquet(out: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        writer = ParquetFileWriter(path=str(out), typename="t", defer_uploads=True)
    await writer.write_batches(_frames())
    await writer.close()


async def _deprecated_json(out: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        writer = JsonFileWriter(path=str(out), typename="t")
    writer._upload_file = _no_upload  # type: ignore[method-assign]  # stage locally, like RollingFileWriter
    await writer.write_batches(_frames())
    await writer.close()


async def _rolling_parquet(out: Path) -> None:
    async with RollingFileWriter[pd.DataFrame](
        str(out), ".parquet", _flush_parquet
    ) as writer:
        for frame in _frames():
            await writer.append(frame)


async def _rolling_json(out: Path) -> None:
    async with RollingFileWriter[list[dict[str, Any]]](
        str(out), ".json", _flush_jsonl
    ) as writer:
        for batch in _row_batches():
            await writer.append(batch)


@pytest.mark.parametrize(
    ("deprecated", "rolling"),
    [
        pytest.param(_deprecated_parquet, _rolling_parquet, id="parquet"),
        pytest.param(_deprecated_json, _rolling_json, id="json"),
    ],
)
async def test_rolling_writer_is_no_slower_than_deprecated_writer(
    tmp_path: Path,
    deprecated: Callable[[Path], Awaitable[None]],
    rolling: Callable[[Path], Awaitable[None]],
) -> None:
    deprecated_s = await _timed(lambda: deprecated(tmp_path / "deprecated"))
    rolling_s = min(
        [
            await _timed(lambda run=run: rolling(tmp_path / f"rolling-{run}"))
            for run in range(ROLLING_RUNS)
        ]
    )

    assert rolling_s <= deprecated_s, (
        f"RollingFileWriter took {rolling_s:.2f}s for {ROWS:,} rows; the "
        f"deprecated writer it replaces took {deprecated_s:.2f}s"
    )
