import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.outputs.json import JsonOutput
from application_sdk.test_utils.hypothesis.strategies.outputs.json_output import (
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
async def test_init(base_output_path: str, config: Dict[str, Any]) -> None:
    # Create a safe output path by joining base_output_path with config's output_path
    safe_path = str(Path(base_output_path) / config["output_path"])
    json_output = JsonOutput(  # type: ignore
        output_path=safe_path,
        output_suffix=config["output_suffix"],
        output_prefix=config["output_prefix"],
        chunk_size=config["chunk_size"],
    )

    assert json_output.output_path.endswith(config["output_suffix"])
    assert json_output.output_prefix == config["output_prefix"]
    assert json_output.chunk_size == config["chunk_size"]
    assert os.path.exists(json_output.output_path)


@pytest.mark.asyncio
async def test_write_dataframe_empty(base_output_path: str) -> None:
    json_output = JsonOutput(  # type: ignore
        output_suffix="tests/raw",
        output_path=base_output_path,
        output_prefix="test_prefix",
        chunk_size=100000,
        typename=None,
        chunk_count=0,
        total_record_count=0,
        chunk_start=None,
    )
    dataframe = pd.DataFrame()
    await json_output.write_dataframe(dataframe)
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0


@pytest.mark.skip(
    reason="Failing due to hypothesis error: Invalid size min_size=-8432831563820742370 < 0"
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(df=dataframe_strategy())  # type: ignore
@pytest.mark.asyncio
async def test_write_dataframe_single_chunk(
    base_output_path: str, df: pd.DataFrame
) -> None:
    with patch(
        "application_sdk.outputs.objectstore.ObjectStoreOutput.push_file_to_object_store"
    ) as mock_push:
        json_output = JsonOutput(  # type: ignore
            output_suffix="tests/raw",
            output_path=base_output_path,
            output_prefix="test_prefix",
            chunk_size=len(df)
            + 1,  # Ensure single chunk by making chunk size larger than df
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )
        await json_output.write_dataframe(df)

        assert json_output.chunk_count == (1 if not df.empty else 0)
        assert json_output.total_record_count == len(df)
        if not df.empty:
            assert os.path.exists(f"{json_output.output_path}/1.json")
            mock_push.assert_called_once()
        else:
            mock_push.assert_not_called()


@pytest.mark.skip(
    reason="Failing due to hypothesis error: Invalid size min_size=-16824 < 0"
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(df=dataframe_strategy(), chunk_size=chunk_size_strategy)  # type: ignore
@pytest.mark.asyncio
async def test_write_dataframe_multiple_chunks(
    base_output_path: str, df: pd.DataFrame, chunk_size: int
) -> None:
    with patch(
        "application_sdk.outputs.objectstore.ObjectStoreOutput.push_file_to_object_store"
    ) as mock_push:
        json_output = JsonOutput(  # type: ignore
            output_suffix="tests/raw",
            output_path=base_output_path,
            output_prefix="test_prefix",
            chunk_size=chunk_size,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )
        await json_output.write_dataframe(df)

        expected_chunks = (
            (len(df) + chunk_size - 1) // chunk_size if not df.empty else 0
        )

        # Check if the files are created on the path json_output.output_path
        for i in range(1, expected_chunks + 1):
            assert os.path.exists(f"{json_output.output_path}/{i}.json")

        assert json_output.chunk_count == expected_chunks
        assert json_output.total_record_count == len(df)
        assert mock_push.call_count == expected_chunks


@pytest.mark.asyncio
async def test_write_dataframe_error(base_output_path: str) -> None:
    json_output = JsonOutput(  # type: ignore
        output_suffix="tests/raw",
        output_path=base_output_path,
        output_prefix="test_prefix",
        chunk_size=100000,
        typename=None,
        chunk_count=0,
        total_record_count=0,
        chunk_start=None,
    )
    invalid_df = "not_a_dataframe"
    await json_output.write_dataframe(invalid_df)  # type: ignore
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0
