import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.storage.formats.json import JsonFileWriter
from application_sdk.testing.hypothesis.strategies.outputs.json_output import (
    chunk_size_strategy,
    dataframe_strategy,
    json_output_config_strategy,
)


@pytest.fixture(scope="module")
def base_output_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Create a module-scoped temporary directory for tests."""
    tmp_path = tmp_path_factory.mktemp("json_output")
    return str(tmp_path / "test_output")


@pytest.mark.skip(
    reason="Failing due to hypothesis error: Invalid size min_size=-16824 < 0"
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(config=json_output_config_strategy)
@pytest.mark.asyncio
async def test_init(base_output_path: str, config: dict[str, Any]) -> None:
    # Create a safe output path by joining base_output_path with config's output_path
    safe_path = str(Path(base_output_path) / config["output_path"])
    json_output = JsonFileWriter(  # type: ignore
        path=safe_path,
        chunk_size=config["chunk_size"],
    )

    assert json_output.path == safe_path
    assert json_output.chunk_size == config["chunk_size"]
    assert os.path.exists(json_output.path)


@pytest.mark.asyncio
async def test_write_empty(base_output_path: str) -> None:
    json_output = JsonFileWriter(  # type: ignore
        path=os.path.join(base_output_path, "tests", "raw"),
        chunk_size=100000,
        typename=None,
        chunk_count=0,
        total_record_count=0,
        chunk_start=None,
    )
    dataframe = pd.DataFrame()
    await json_output.write(dataframe)
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0


@pytest.mark.skip(
    reason="Failing due to hypothesis error: Invalid size min_size=-8432831563820742370 < 0"
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(df=dataframe_strategy())  # type: ignore
@pytest.mark.asyncio
async def test_write_single_chunk(base_output_path: str, df: pd.DataFrame) -> None:
    with patch("application_sdk.storage.formats._upload_file") as mock_push:
        json_output = JsonFileWriter(  # type: ignore
            path=os.path.join(base_output_path, "tests", "raw"),
            chunk_size=len(df)
            + 1,  # Ensure single chunk by making chunk size larger than df
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )
        await json_output.write(df)

        assert json_output.chunk_count == (1 if not df.empty else 0)
        assert json_output.total_record_count == len(df)
        if not df.empty:
            assert os.path.exists(f"{json_output.path}/1.json")
            mock_push.assert_called_once()
        else:
            mock_push.assert_not_called()


@pytest.mark.skip(
    reason="Failing due to hypothesis error: Invalid size min_size=-16824 < 0"
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(df=dataframe_strategy(), chunk_size=chunk_size_strategy)  # type: ignore
@pytest.mark.asyncio
async def test_write_multiple_chunks(
    base_output_path: str, df: pd.DataFrame, chunk_size: int
) -> None:
    with patch("application_sdk.storage.formats._upload_file") as mock_push:
        json_output = JsonFileWriter(  # type: ignore
            path=os.path.join(base_output_path, "tests", "raw"),
            chunk_size=chunk_size,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )
        await json_output.write(df)

        expected_chunks = (
            (len(df) + chunk_size - 1) // chunk_size if not df.empty else 0
        )

        # Check if the files are created on the path json_output.output_path
        for i in range(1, expected_chunks + 1):
            assert os.path.exists(f"{json_output.path}/{i}.json")

        assert json_output.chunk_count == expected_chunks
        assert json_output.total_record_count == len(df)
        assert mock_push.call_count == expected_chunks


@pytest.mark.asyncio
async def test_write_error(base_output_path: str) -> None:
    json_output = JsonFileWriter(  # type: ignore
        path=os.path.join(base_output_path, "tests", "raw"),
        chunk_size=100000,
        typename=None,
        chunk_count=0,
        total_record_count=0,
        chunk_start=None,
    )
    from application_sdk.storage.formats.format_errors import UnsupportedDataTypeError

    invalid_df = "not_a_dataframe"
    with pytest.raises(UnsupportedDataTypeError) as exc_info:
        await json_output.write(invalid_df)  # type: ignore
    assert exc_info.value.code == "INVALID_INPUT_FORMAT_DATA_TYPE"
    # Verify counts remain unchanged after error
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0


# ---------------------------------------------------------------------------
# Compat shim: DataframeType.daft constructor deprecation
# ---------------------------------------------------------------------------


def test_init_daft_dataframe_type_emits_deprecation_and_routes_to_pandas(
    tmp_path: Path,
) -> None:
    """JsonFileWriter(dataframe_type=DataframeType.daft) must warn and route to pandas."""
    import warnings as _warnings

    from application_sdk.common.types import DataframeType

    with _warnings.catch_warnings(record=True) as captured:
        _warnings.simplefilter("always")
        writer = JsonFileWriter(
            path=str(tmp_path / "out"),
            dataframe_type=DataframeType.daft,
        )

    assert writer.dataframe_type == DataframeType.pandas
    assert any(
        issubclass(w.category, DeprecationWarning)
        and "DataframeType.daft is deprecated" in str(w.message)
        for w in captured
    )


# ---------------------------------------------------------------------------
# orjson.dumps: pandas.Timestamp regression (Fix BLDX-1470)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_chunk_handles_pandas_timestamp(tmp_path: Path) -> None:
    """_write_chunk must not raise TypeError when a column contains pandas.Timestamp."""
    import pandas as pd

    writer = JsonFileWriter(path=str(tmp_path / "out"))
    df = pd.DataFrame(
        {
            "name": ["a"],
            "ts": [pd.Timestamp("2024-01-01T00:00:00")],
        }
    )
    out_file = str(tmp_path / "out" / "chunk.json")
    # Must not raise TypeError: Type is not JSON serializable: Timestamp
    await writer._write_chunk(df, out_file)

    import orjson

    with open(out_file, "rb") as f:
        record = orjson.loads(f.read().strip())
    assert record["name"] == "a"
    assert "2024-01-01" in record["ts"]


@pytest.mark.asyncio
async def test_write_chunk_preserves_nested_object_structure(tmp_path: Path) -> None:
    """Nested model objects must round-trip as JSON objects, not str(obj).

    Regression: the Looker ``Dialect`` SDK model (and any object with __dict__)
    was flattened to its string repr by the ``str(obj)`` fallback, corrupting
    the record and crashing downstream path access ("dialect/name").
    """
    import dataclasses as _dc

    import orjson
    import pandas as pd

    @_dc.dataclass
    class Dialect:
        name: str
        label: str

    class Plain:
        def __init__(self) -> None:
            self.name = "postgres"

    writer = JsonFileWriter(path=str(tmp_path / "out"))
    df = pd.DataFrame(
        {
            "conn": ["c1", "c2"],
            "dialect": [Dialect(name="postgres", label="PostgreSQL"), Plain()],
            "ts": [pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02")],
        }
    )
    out_file = str(tmp_path / "out" / "chunk.json")
    await writer._write_chunk(df, out_file)

    with open(out_file, "rb") as f:
        records = [orjson.loads(line) for line in f.read().splitlines() if line.strip()]

    # dataclass -> nested object; "dialect/name" is now traversable
    assert records[0]["dialect"] == {"name": "postgres", "label": "PostgreSQL"}
    assert records[0]["dialect"]["name"] == "postgres"
    # plain object with __dict__ -> nested object
    assert records[1]["dialect"] == {"name": "postgres"}
    # scalar datetime-like still stringified (unchanged behaviour)
    assert isinstance(records[0]["ts"], str) and "2024-01-01" in records[0]["ts"]
