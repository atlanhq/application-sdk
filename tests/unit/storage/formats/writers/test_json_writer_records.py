"""JsonFileWriter writes ``list[dict]`` input as lines, with the same output as a DataFrame.

Records whose values pandas would hand back unchanged are written without
building a DataFrame of the batch; every other input takes the DataFrame path.
Everything a reader or the object store sees must be the same on both paths,
so these tests compare the records path with the DataFrame path — reachable as
``Writer.write`` — on the same input:

* every published file: name, bytes, and the statistics sidecar;
* every upload key, in order;
* ``total_record_count``, ``chunk_count`` and ``partitions``.

The golden file pins those outputs as the DataFrame path produces them, so a
change to the shared DataFrame path is caught too.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import os
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from application_sdk.storage.formats import Writer
from application_sdk.storage.formats.json import JsonFileWriter

_GOLDEN = Path(__file__).parent / "fixtures" / "json_writer_records_golden.json"


def _records_scenarios() -> dict[str, list[Any]]:
    """Named sequences of ``write()`` arguments, one writer per scenario."""
    uniform = [
        {
            "typeName": "Column",
            "name": f"col_{i}",
            "order": i,
            "ratio": i / 7,
            "isNullable": bool(i % 2),
            "attributes": {"qualifiedName": f"default/x/1/db/sch/t/col_{i}"},
            "tags": ["a", "b"] if i % 3 else [],
        }
        for i in range(60)
    ]
    ragged = [
        {
            "typeName": "Table",
            "name": f"t_{i}",
            **({"description": 'é 中文 "quoted" / slash'} if i % 4 == 0 else {}),
            **({"rowCount": float(i * 10)} if i % 5 else {"rowCount": None}),
            **({"extra": {"k": i}} if i % 6 == 0 else {}),
        }
        for i in range(45)
    ]
    reordered = [{"b": 1, "a": "x"}, {"a": "y", "b": 2}, {"c": True, "a": "z", "b": 3}]
    # Shapes pandas does change on the way through: each must take the
    # DataFrame path and come out exactly as it always has.
    int_with_none = [{"id": 1, "n": 3}, {"id": 2, "n": None}, {"id": 3}]
    int_with_float = [{"v": 1}, {"v": 2.5}]
    timestamps = [
        {"at": dt.datetime(2024, 1, 2, 3, 4, 5), "d": dt.date(2024, 1, 2)},
        {"at": dt.datetime(2025, 6, 7, 8, 9, 10), "d": dt.date(2025, 6, 7)},
    ]
    nan_in_strings = [{"s": "a"}, {"s": math.nan}]
    return {
        "uniform_in_three_writes": [uniform[:25], uniform[25:50], uniform[50:]],
        "ragged_keys": [ragged],
        "reordered_keys": [reordered],
        "single_dict": [{"typeName": "Database", "name": "db1"}],
        "int_with_none": [int_with_none],
        "int_with_float": [int_with_float],
        "timestamps": [timestamps],
        "nan_in_strings": [nan_in_strings],
        # No columns at all: the DataFrame counts as empty and writes nothing.
        "empty_dict": [{}],
        "list_of_empty_dicts": [[{}, {}], [{"name": "a"}]],
        "mixed_with_a_dataframe": [
            uniform[:5],
            pd.DataFrame(uniform[5:10]),
            ragged[:5],
        ],
    }


@contextmanager
def _captured_uploads() -> Iterator[list[str]]:
    keys: list[str] = []

    async def _upload(published: str, staged: str, **_: Any) -> None:
        keys.append(published)

    with patch(
        "application_sdk.storage.formats._upload_file",
        new=AsyncMock(side_effect=_upload),
    ):
        yield keys


def _new_writer(root: Path, *, max_file_size_bytes: int, buffer_size: int):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        writer = JsonFileWriter(path=str(root), typename="t", buffer_size=buffer_size)
    writer.max_file_size_bytes = max_file_size_bytes
    return writer


async def _run(
    root: Path,
    writes: list[Any],
    *,
    via_dataframe: bool,
    max_file_size_bytes: int = 1_500,
    buffer_size: int = 7,
) -> dict[str, Any]:
    """Drive one writer and return everything a consumer could observe."""
    with _captured_uploads() as keys:
        writer = _new_writer(
            root, max_file_size_bytes=max_file_size_bytes, buffer_size=buffer_size
        )
        for data in writes:
            if via_dataframe:
                await Writer.write(writer, data)
            else:
                await writer.write(data)
        result = await writer.close()

    out = Path(writer.path)
    files = {
        p.relative_to(out).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }
    return {
        "files": files,
        "uploads": [os.path.relpath(k, out).replace(os.sep, "/") for k in keys],
        "total_record_count": result.total_record_count,
        "chunk_count": result.chunk_count,
        "partitions": result.partitions,
    }


@pytest.mark.parametrize("scenario", sorted(_records_scenarios()))
async def test_output_matches_the_golden_from_the_dataframe_writer(
    tmp_path: Path, scenario: str
) -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))[scenario]

    observed = await _run(
        tmp_path / scenario, _records_scenarios()[scenario], via_dataframe=False
    )

    assert observed == golden


@pytest.mark.parametrize("scenario", sorted(_records_scenarios()))
async def test_output_matches_the_dataframe_path_on_the_same_input(
    tmp_path: Path, scenario: str
) -> None:
    writes = _records_scenarios()[scenario]

    old = await _run(tmp_path / "old", writes, via_dataframe=True)
    new = await _run(tmp_path / "new", writes, via_dataframe=False)

    assert new == old


async def test_records_are_written_without_a_dataframe_of_the_batch(
    tmp_path: Path,
) -> None:
    records = [{"name": f"col_{i}", "order": i, "ratio": i / 3} for i in range(500)]
    built: list[int] = []
    real_dataframe = pd.DataFrame

    def _spy(*args: Any, **kwargs: Any) -> pd.DataFrame:
        frame = real_dataframe(*args, **kwargs)
        built.append(len(frame))
        return frame

    with (
        _captured_uploads(),
        patch("pandas.DataFrame", side_effect=_spy),
    ):
        writer = _new_writer(tmp_path, max_file_size_bytes=10**8, buffer_size=5000)
        await writer.write(records)
        await writer.close()

    # Only the size estimate's ten-row sample may become a frame.
    assert max(built, default=0) <= 10, built
    assert writer.total_record_count == 500


async def test_a_subclass_that_overrides_the_chunk_write_still_gets_a_dataframe(
    tmp_path: Path,
) -> None:
    seen: list[type] = []

    class _Tagging(JsonFileWriter):
        async def _write_chunk(self, chunk: Any, file_name: str) -> None:
            seen.append(type(chunk))
            await super()._write_chunk(chunk, file_name)

    with _captured_uploads():
        writer = _Tagging(path=str(tmp_path), typename="t", buffer_size=5000)
        await writer.write([{"name": "a"}, {"name": "b"}])
        await writer.close()

    assert seen == [pd.DataFrame]
    assert writer.total_record_count == 2


async def test_an_instance_with_a_patched_step_still_gets_a_dataframe(
    tmp_path: Path,
) -> None:
    writer = _new_writer(tmp_path, max_file_size_bytes=10**8, buffer_size=5000)
    real_write_chunk = writer._write_chunk
    seen: list[type] = []

    async def _spy(chunk: Any, file_name: str) -> None:
        seen.append(type(chunk))
        await real_write_chunk(chunk, file_name)

    with _captured_uploads(), patch.object(writer, "_write_chunk", side_effect=_spy):
        await writer.write([{"name": "a"}, {"name": "b"}])
        await writer.close()

    assert seen == [pd.DataFrame]


async def test_the_records_are_taken_when_write_is_called(tmp_path: Path) -> None:
    buffer = [{"name": f"n{i}", "meta": {"i": i}} for i in range(6)]

    with _captured_uploads():
        writer = _new_writer(tmp_path, max_file_size_bytes=10**8, buffer_size=5000)
        task = asyncio.create_task(writer.write(buffer))
        await asyncio.sleep(0)
        buffer[0]["name"] = "changed"
        buffer.clear()
        await task
        await writer.close()

    lines = (Path(writer.path) / "chunk-0-part0.json").read_text().splitlines()
    assert [json.loads(line)["name"] for line in lines] == [f"n{i}" for i in range(6)]
    assert writer.total_record_count == 6


# ── property: any batch of plain records, any split points ──────────────────

_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=12),
)
_value = st.one_of(
    _scalar,
    st.lists(_scalar, max_size=3),
    st.dictionaries(st.text(max_size=4), _scalar, max_size=3),
    st.datetimes(min_value=dt.datetime(1970, 1, 2), max_value=dt.datetime(2200, 1, 1)),
)
_keys = st.sampled_from(["a", "b", "c", "d", "name", "é"])
_records = st.lists(
    st.dictionaries(_keys, _value, min_size=1, max_size=6), min_size=1, max_size=40
)
# Values drawn from one type per key, the common case for a connector.
_typed_records = st.lists(
    st.fixed_dictionaries(
        {"name": st.text(max_size=10)},
        optional={
            "n": st.integers(min_value=-(2**40), max_value=2**40),
            "f": st.floats(allow_nan=False),
            "ok": st.booleans(),
            "nested": st.dictionaries(st.text(max_size=3), _scalar, max_size=2),
        },
    ),
    min_size=1,
    max_size=40,
)


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    batches=st.lists(st.one_of(_records, _typed_records), min_size=1, max_size=3),
    max_file_size_bytes=st.integers(min_value=50, max_value=5_000),
    buffer_size=st.integers(min_value=1, max_value=15),
)
async def test_any_plain_records_match_the_dataframe_path(
    tmp_path_factory: pytest.TempPathFactory,
    batches: list[list[dict[str, Any]]],
    max_file_size_bytes: int,
    buffer_size: int,
) -> None:
    root = tmp_path_factory.mktemp("prop")
    kwargs = {"max_file_size_bytes": max_file_size_bytes, "buffer_size": buffer_size}

    old_error = new_error = None
    try:
        old = await _run(root / "old", batches, via_dataframe=True, **kwargs)
    except Exception as exc:  # noqa: BLE001 — compared below, not swallowed
        old, old_error = None, type(exc)
    try:
        new = await _run(root / "new", batches, via_dataframe=False, **kwargs)
    except Exception as exc:  # noqa: BLE001 — compared below, not swallowed
        new, new_error = None, type(exc)

    assert new_error == old_error
    assert new == old
