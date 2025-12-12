import os
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.common.types import DataframeType
from application_sdk.io.json import JsonFileWriter
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
    json_output = JsonFileWriter(  # type: ignore
        output_path=safe_path,
        chunk_size=config["chunk_size"],
    )

    assert json_output.output_path == safe_path
    assert json_output.chunk_size == config["chunk_size"]
    assert os.path.exists(json_output.output_path)


@pytest.mark.asyncio
async def test_write_empty(base_output_path: str) -> None:
    json_output = JsonFileWriter(  # type: ignore
        output_path=os.path.join(base_output_path, "tests", "raw"),
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
    with patch(
        "application_sdk.services.objectstore.ObjectStore.upload_file"
    ) as mock_push:
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "tests", "raw"),
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
async def test_write_multiple_chunks(
    base_output_path: str, df: pd.DataFrame, chunk_size: int
) -> None:
    with patch(
        "application_sdk.services.objectstore.ObjectStore.upload_file"
    ) as mock_push:
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "tests", "raw"),
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
            assert os.path.exists(f"{json_output.output_path}/{i}.json")

        assert json_output.chunk_count == expected_chunks
        assert json_output.total_record_count == len(df)
        assert mock_push.call_count == expected_chunks


@pytest.mark.asyncio
async def test_write_error(base_output_path: str) -> None:
    json_output = JsonFileWriter(  # type: ignore
        output_path=os.path.join(base_output_path, "tests", "raw"),
        chunk_size=100000,
        typename=None,
        chunk_count=0,
        total_record_count=0,
        chunk_start=None,
    )
    invalid_df = "not_a_dataframe"
    with pytest.raises(AttributeError, match="'str' object has no attribute"):
        await json_output.write(invalid_df)  # type: ignore
    # Verify counts remain unchanged after error
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0


class TestJsonFileWriterDict:
    """Test cases for JsonFileWriter dictionary handling."""

    @pytest.mark.asyncio
    async def test_write_single_dict(self, base_output_path: str) -> None:
        """Test writing a single dictionary."""
        with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
            writer = JsonFileWriter(
                output_path=os.path.join(base_output_path, "test_single"),
                chunk_size=100,
                dataframe_type=DataframeType.dict,
            )
            data = {"id": 1, "name": "test"}
            await writer.write(data)

            assert writer.total_record_count == 1

            stats = await writer.get_statistics()
            assert stats.total_record_count == 1

    @pytest.mark.asyncio
    async def test_write_list_of_dicts(self, base_output_path: str) -> None:
        """Test writing a list of dictionaries."""
        with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
            output_path = os.path.join(base_output_path, "test_list")
            writer = JsonFileWriter(
                output_path=output_path,
                chunk_size=100,
                dataframe_type=DataframeType.dict,
            )
            data = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
            await writer.write(data)

            stats = await writer.get_statistics()
            assert stats.total_record_count == 2

            # Check files in directory
            files = os.listdir(output_path)
            # Filter for .json files
            json_files = [f for f in files if f.endswith(".json")]
            assert len(json_files) >= 1

    @pytest.mark.asyncio
    async def test_write_empty_dict(self, base_output_path: str) -> None:
        """Test writing an empty dictionary list."""
        with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
            writer = JsonFileWriter(
                output_path=os.path.join(base_output_path, "test_empty"),
                chunk_size=100,
                dataframe_type=DataframeType.dict,
            )
            data: list = []
            await writer.write(data)

            assert writer.total_record_count == 0
            stats = await writer.get_statistics()
            assert stats.total_record_count == 0

    @pytest.mark.asyncio
    async def test_write_batches_dicts(self, base_output_path: str) -> None:
        """Test writing batches of dictionaries."""
        with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
            writer = JsonFileWriter(
                output_path=os.path.join(base_output_path, "test_batches"),
                chunk_size=100,
                dataframe_type=DataframeType.dict,
            )

            def dict_generator():
                yield {"id": 1}
                yield [{"id": 2}, {"id": 3}]

            await writer.write_batches(dict_generator())

            stats = await writer.get_statistics()
            assert stats.total_record_count == 3

    @pytest.mark.asyncio
    async def test_write_batches_empty_dicts(self, base_output_path: str) -> None:
        """Test writing batches with empty dictionary lists."""
        with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
            writer = JsonFileWriter(
                output_path=os.path.join(base_output_path, "test_batches_empty"),
                chunk_size=100,
                dataframe_type=DataframeType.dict,
            )

            def dict_generator():
                yield {"id": 1}
                yield []  # Empty list should be skipped
                yield [{"id": 2}]

            await writer.write_batches(dict_generator())

            stats = await writer.get_statistics()
            assert stats.total_record_count == 2

    @pytest.mark.asyncio
    async def test_write_dict_with_preserve_fields(self, base_output_path: str) -> None:
        """Test writing dictionary with preserve_fields parameter."""
        with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
            writer = JsonFileWriter(
                output_path=os.path.join(base_output_path, "test_preserve"),
                chunk_size=100,
                dataframe_type=DataframeType.dict,
            )
            data = [{"id": 1, "identity_cycle": None, "name": "test"}]
            await writer.write(data, preserve_fields=["identity_cycle", "custom_field"])

            assert writer.total_record_count == 1
