"""Tests for the row-to-row record streaming helpers."""

from __future__ import annotations

import asyncio
import gc
import json
import time
import tracemalloc
from pathlib import Path
from typing import Any

import orjson
import pytest

from application_sdk.storage.streaming import (
    DEFAULT_BATCH_BYTES,
    MapResult,
    batched_by_bytes,
    iter_jsonl,
    iter_jsonl_batches,
    map_jsonl,
    map_records,
    write_records,
)
from application_sdk.storage.streaming_errors import InvalidBatchBudgetError


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in records))
    return path


def _write_n_rows(path: Path, rows: int) -> Path:
    with open(path, "wb") as fh:
        for i in range(rows):
            fh.write(b'{"id":%d,"name":"table_%d","schema":"sch"}\n' % (i, i))
    return path


# ── iter_jsonl ──────────────────────────────────────────────────────────────


def test_iter_jsonl_skips_blank_lines_and_reads_a_last_line_without_newline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "in.jsonl"
    path.write_bytes(b'{"a":1}\n\n   \n{"a":2}\r\n{"a":3}')

    assert list(iter_jsonl(path)) == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_iter_jsonl_reads_several_files_in_the_order_given(tmp_path: Path) -> None:
    first = _write_jsonl(tmp_path / "b.jsonl", [{"n": 1}, {"n": 2}])
    second = _write_jsonl(tmp_path / "a.jsonl", [{"n": 3}])

    assert [r["n"] for r in iter_jsonl([first, str(second)])] == [1, 2, 3]


def test_iter_jsonl_raises_on_a_line_that_is_not_json(tmp_path: Path) -> None:
    path = tmp_path / "in.jsonl"
    path.write_bytes(b'{"a":1}\nnot json\n')

    with pytest.raises(orjson.JSONDecodeError):
        list(iter_jsonl(path))


# ── batched_by_bytes / iter_jsonl_batches ───────────────────────────────────


def test_batched_by_bytes_closes_a_batch_before_it_would_pass_the_budget() -> None:
    items = ["aaaa", "bb", "cccc", "d", "eeeeee"]

    batches = list(batched_by_bytes(items, max_bytes=6, size_of=len))

    assert batches == [["aaaa", "bb"], ["cccc", "d"], ["eeeeee"]]


def test_batched_by_bytes_gives_an_oversized_item_a_batch_of_its_own() -> None:
    batches = list(batched_by_bytes(["ab", "x" * 10, "cd"], max_bytes=4, size_of=len))

    assert batches == [["ab"], ["x" * 10], ["cd"]]


def test_batched_by_bytes_yields_nothing_for_no_items() -> None:
    assert list(batched_by_bytes([], max_bytes=10, size_of=len)) == []


def test_batched_by_bytes_sizes_records_by_their_serialized_length_by_default() -> None:
    record = {"name": "x" * 40}
    one = len(orjson.dumps(record)) + 1

    batches = list(batched_by_bytes([record] * 5, max_bytes=one * 2))

    assert [len(b) for b in batches] == [2, 2, 1]


@pytest.mark.parametrize("budget", [0, -1])
def test_batched_by_bytes_rejects_a_budget_that_admits_nothing(budget: int) -> None:
    with pytest.raises(InvalidBatchBudgetError):
        list(batched_by_bytes(["a"], max_bytes=budget, size_of=len))


def test_iter_jsonl_batches_sizes_batches_by_the_bytes_on_disk(tmp_path: Path) -> None:
    records = [{"id": i, "pad": "p" * (i % 50)} for i in range(500)]
    path = _write_jsonl(tmp_path / "in.jsonl", records)
    budget = 2_000

    batches = list(iter_jsonl_batches(path, max_bytes=budget))

    assert [r for batch in batches for r in batch] == records
    for batch in batches:
        assert sum(len(orjson.dumps(r)) + 1 for r in batch) <= budget
    assert len(batches) > 1


def test_default_budget_is_tens_of_mebibytes_not_a_row_count() -> None:
    assert 32 * 1024 * 1024 <= DEFAULT_BATCH_BYTES <= 64 * 1024 * 1024


# ── map_records / map_jsonl ─────────────────────────────────────────────────


def test_map_records_writes_one_line_per_result_and_drops_none(tmp_path: Path) -> None:
    dst = tmp_path / "out" / "entities.json"

    def fn(record: dict[str, Any]) -> dict[str, Any] | bytes | None:
        if record["id"] == 1:
            return None
        if record["id"] == 2:
            return b'{"pre":"serialized"}'
        return {"typeName": "Table", "id": record["id"]}

    result = map_records([{"id": 0}, {"id": 1}, {"id": 2}], dst, fn)

    assert result == MapResult(read=3, written=2)
    assert dst.read_bytes() == (b'{"typeName":"Table","id":0}\n{"pre":"serialized"}\n')


def test_map_records_serializes_decimal_and_bytes_like_the_extract_side(
    tmp_path: Path,
) -> None:
    from decimal import Decimal

    dst = tmp_path / "out.json"

    map_records([{"n": Decimal("1.5"), "b": b"xy"}], dst, lambda r: r)

    assert json.loads(dst.read_text()) == {"n": 1.5, "b": "xy"}


def test_map_records_leaves_no_output_when_the_mapper_fails(tmp_path: Path) -> None:
    dst = tmp_path / "out.json"
    dst.write_bytes(b"previous attempt\n")

    def fn(record: dict[str, Any]) -> dict[str, Any]:
        if record["id"] == 3:
            raise ValueError("bad row")
        return record

    with pytest.raises(ValueError, match="bad row"):
        map_records(({"id": i} for i in range(10)), dst, fn)

    # The final name holds either a complete file or what was there before.
    assert dst.read_bytes() == b"previous attempt\n"


def test_map_records_writes_an_empty_file_for_no_records(tmp_path: Path) -> None:
    dst = tmp_path / "out.json"

    assert map_records([], dst, lambda r: r) == MapResult(read=0, written=0)
    assert dst.read_bytes() == b""


async def test_map_jsonl_maps_a_file_off_the_event_loop(tmp_path: Path) -> None:
    src = _write_n_rows(tmp_path / "raw.json", 2_500)
    dst = tmp_path / "transformed" / "entities.json"

    result = await map_jsonl(src, dst, lambda r: {"qualifiedName": r["name"]})

    assert result == MapResult(read=2_500, written=2_500)
    lines = dst.read_bytes().splitlines()
    assert len(lines) == 2_500
    assert orjson.loads(lines[-1]) == {"qualifiedName": "table_2499"}


# ── memory stays flat with input size ───────────────────────────────────────


def _peak_traced_bytes_while_mapping(tmp_path: Path, rows: int) -> int:
    src = _write_n_rows(tmp_path / f"raw-{rows}.json", rows)
    dst = tmp_path / f"out-{rows}.json"
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        result = map_records(
            iter_jsonl(src), dst, lambda r: {"typeName": "Table", "name": r["name"]}
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.written == rows
    return peak


def test_mapping_memory_does_not_grow_with_the_number_of_rows(tmp_path: Path) -> None:
    small = _peak_traced_bytes_while_mapping(tmp_path, 5_000)
    large = _peak_traced_bytes_while_mapping(tmp_path, 100_000)

    # 20x the rows: a path that holds the records (or a DataFrame of them)
    # grows ~20x; a row-to-row stream holds one record and the file buffers.
    assert large < small * 2 + 256 * 1024, (small, large)


@pytest.mark.perf
def test_a_million_row_map_holds_memory_flat(tmp_path: Path) -> None:
    small = _peak_traced_bytes_while_mapping(tmp_path, 10_000)
    large = _peak_traced_bytes_while_mapping(tmp_path, 1_000_000)

    assert large < small * 2 + 256 * 1024, (small, large)
    assert large < 4 * 1024 * 1024, large


def test_write_records_streams_into_a_handle_the_caller_owns(tmp_path: Path) -> None:
    dst = tmp_path / "out.json"

    with open(dst, "wb") as out:
        out.write(b"header\n")
        result = write_records(iter([{"a": 1}, {"a": 2}]), out, lambda r: r)
        out.write(b"footer\n")

    assert result == MapResult(read=2, written=2)
    assert dst.read_bytes() == b'header\n{"a":1}\n{"a":2}\nfooter\n'


def test_write_records_writes_a_str_result_as_an_already_serialized_line(
    tmp_path: Path,
) -> None:
    dst = tmp_path / "out.json"

    with open(dst, "wb") as out:
        write_records(iter([{"a": "é"}]), out, lambda r: orjson.dumps(r).decode())

    assert dst.read_bytes() == '{"a":"é"}\n'.encode()


async def test_a_cancelled_map_stops_and_leaves_the_previous_output_in_place(
    tmp_path: Path,
) -> None:
    src = _write_n_rows(tmp_path / "raw.json", 20_000)
    dst = tmp_path / "out" / "entities.json"
    dst.parent.mkdir()
    dst.write_bytes(b"previous run\n")
    mapped: list[int] = []

    def _slow(record: dict[str, Any]) -> dict[str, Any]:
        mapped.append(1)
        time.sleep(0.0002)
        return record

    task = asyncio.create_task(map_jsonl(src, dst, _slow))
    while len(mapped) < 2_000:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The worker thread runs on until its next check; wait for it to stop.
    settled, last = 0, -1
    while settled < 5:
        await asyncio.sleep(0.05)
        settled, last = (settled + 1, last) if len(mapped) == last else (0, len(mapped))

    assert len(mapped) < 20_000
    assert dst.read_bytes() == b"previous run\n"
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert sorted(p.name for p in leftovers) == ["entities.json", "raw.json"]
