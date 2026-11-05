"""TEMPORARY diagnostic for FND-4729. Removed in the fix commit.

Finds which step leaves a converted parquet file undeletable on Windows
(WinError 32): the converter's write, a live ``pd.read_parquet`` frame, or
something outside the process. For each blocked unlink it records how long
the block lasted before a retry succeeded, and whether anything in-process
changed in the meantime.
"""

import gc
import os
import time
from pathlib import Path

import pandas as pd

from application_sdk.common.file_converter import convert_json_to_parquet

ITERATIONS = 25
_ROWS = "".join(
    f'{{"id": {i}, "name": "n{i}", "age": {20 + i}, "active": true}}\n'
    for i in range(3)
)


def _convert(directory: Path, stem: str) -> Path:
    source = directory / f"{stem}.json"
    source.write_text(_ROWS)
    result = convert_json_to_parquet(str(source))
    assert result is not None
    return Path(result)


def _unlink_with_retry(path: Path) -> float | None:
    """None if the first unlink worked, else seconds until one did."""
    try:
        os.unlink(path)
        return None
    except PermissionError:
        start = time.monotonic()
    while True:
        time.sleep(0.01)
        try:
            os.unlink(path)
            return time.monotonic() - start
        except PermissionError:
            if time.monotonic() - start > 5:
                return float("inf")


def test_where_winerror32_comes_from(tmp_path):
    blocked: dict[str, list[float]] = {
        "after_write": [],
        "df_alive": [],
        "after_del_gc": [],
        "json_input": [],
    }
    for i in range(ITERATIONS):
        directory = tmp_path / str(i)
        directory.mkdir()

        written = _convert(directory, "a")
        if (d := _unlink_with_retry(written)) is not None:
            blocked["after_write"].append(d)
        if (d := _unlink_with_retry(directory / "a.json")) is not None:
            blocked["json_input"].append(d)

        alive = _convert(directory, "b")
        df = pd.read_parquet(alive)
        assert len(df) == 3
        if (d := _unlink_with_retry(alive)) is not None:
            blocked["df_alive"].append(d)
        del df

        collected = _convert(directory, "c")
        df = pd.read_parquet(collected)
        assert len(df) == 3
        del df
        gc.collect()
        if (d := _unlink_with_retry(collected)) is not None:
            blocked["after_del_gc"].append(d)

    report = {
        k: f"{len(v)}/{ITERATIONS} blocked, max {max(v):.3f}s" if v else "0"
        for k, v in blocked.items()
    }
    assert not any(blocked.values()), f"WinError 32 probe: {report}"
