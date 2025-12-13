import os
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Generator, List, Union
from unittest.mock import patch

import orjson
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings

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


# Tests for write_dict functionality
class TestJsonFileWriterWriteDict:
    """Test suite for JsonFileWriter.write_dict() method."""

    @pytest.mark.asyncio
    async def test_write_dict_single_dict(self, base_output_path: str) -> None:
        """Test writing a single dictionary produces one JSONL line."""
        with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
            json_output = JsonFileWriter(  # type: ignore
                output_path=os.path.join(base_output_path, "test_dict_single"),
                chunk_size=100000,
                buffer_size=10,
                typename=None,
                chunk_count=0,
                total_record_count=0,
                chunk_start=None,
            )
            test_dict = {"id": 1, "name": "test", "value": 42}
            await json_output.write_dict(test_dict)

            assert json_output.total_record_count == 1
            # Check file was created
            output_file = f"{json_output.output_path}/chunk-0-part0.json"
            assert os.path.exists(output_file)

            # Read and verify content
            with open(output_file, "rb") as f:
                content = f.read()
                decoded = orjson.loads(content)
                assert decoded == test_dict

    @pytest.mark.asyncio
    async def test_write_dict_list_of_dicts(self, base_output_path: str) -> None:
        """Test writing a list of dictionaries produces multiple JSONL lines."""
        with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
            json_output = JsonFileWriter(  # type: ignore
                output_path=os.path.join(base_output_path, "test_dict_list"),
                chunk_size=100000,
                buffer_size=10,
                typename=None,
                chunk_count=0,
                total_record_count=0,
                chunk_start=None,
            )
            test_dicts = [
                {"id": 1, "name": "test1"},
                {"id": 2, "name": "test2"},
                {"id": 3, "name": "test3"},
            ]
            await json_output.write_dict(test_dicts)

            assert json_output.total_record_count == 3
            # Check file was created
            output_file = f"{json_output.output_path}/chunk-0-part0.json"
            assert os.path.exists(output_file)

            # Read and verify content (JSONL format - one JSON object per line)
            with open(output_file, "rb") as f:
                lines = f.readlines()
                assert len(lines) == 3
                for i, line in enumerate(lines):
                    decoded = orjson.loads(line)
                    assert decoded == test_dicts[i]

    @pytest.mark.asyncio
    async def test_write_dict_invalid_list_contents(
        self, base_output_path: str
    ) -> None:
        """Test write_dict raises TypeError when list contains non-dict items."""
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "test_dict_invalid"),
            chunk_size=100000,
            buffer_size=10,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )
        # Test with list of integers
        with pytest.raises(
            TypeError, match="Expected list\\[dict\\], but list contains non-dict items"
        ):
            await json_output.write_dict([1, 2, 3])

        # Test with mixed types
        with pytest.raises(
            TypeError, match="Expected list\\[dict\\], but list contains non-dict items"
        ):
            await json_output.write_dict([{"id": 1}, "not_a_dict", {"id": 2}])

    @pytest.mark.asyncio
    async def test_write_dict_empty_list(self, base_output_path: str) -> None:
        """Test write_dict with empty list does nothing and logs."""
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "test_dict_empty"),
            chunk_size=100000,
            buffer_size=10,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )
        # Should not raise error, just return early
        await json_output.write_dict([])
        assert json_output.total_record_count == 0
        # No file should be created
        assert not os.path.exists(f"{json_output.output_path}/chunk-1-part0.json")


class TestJsonFileWriterWriteBatchedDict:
    """Test suite for JsonFileWriter.write_batched_dict() method."""

    @pytest.mark.asyncio
    async def test_write_batched_dict_sync_generator_dict(
        self, base_output_path: str
    ) -> None:
        """Test sync generator yielding dict."""
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "test_batched_sync_dict"),
            chunk_size=100000,
            buffer_size=10,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )

        def sync_gen() -> Generator[Dict[str, Any], None, None]:
            yield {"id": 1, "name": "test1"}
            yield {"id": 2, "name": "test2"}
            yield {"id": 3, "name": "test3"}

        await json_output.write_batched_dict(sync_gen())

        assert json_output.total_record_count == 3
        output_file = f"{json_output.output_path}/chunk-0-part0.json"
        assert os.path.exists(output_file)

    @pytest.mark.asyncio
    async def test_write_batched_dict_sync_generator_list(
        self, base_output_path: str
    ) -> None:
        """Test sync generator yielding list[dict]."""
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "test_batched_sync_list"),
            chunk_size=100000,
            buffer_size=10,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )

        def sync_gen() -> Generator[List[Dict[str, Any]], None, None]:
            yield [{"id": 1, "name": "test1"}, {"id": 2, "name": "test2"}]
            yield [{"id": 3, "name": "test3"}]

        await json_output.write_batched_dict(sync_gen())

        assert json_output.total_record_count == 3
        output_file = f"{json_output.output_path}/chunk-0-part0.json"
        assert os.path.exists(output_file)

    @pytest.mark.asyncio
    async def test_write_batched_dict_async_generator_dict(
        self, base_output_path: str
    ) -> None:
        """Test async generator yielding dict."""
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "test_batched_async_dict"),
            chunk_size=100000,
            buffer_size=10,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )

        async def async_gen() -> AsyncGenerator[Dict[str, Any], None]:
            yield {"id": 1, "name": "test1"}
            yield {"id": 2, "name": "test2"}
            yield {"id": 3, "name": "test3"}

        await json_output.write_batched_dict(async_gen())

        assert json_output.total_record_count == 3
        output_file = f"{json_output.output_path}/chunk-0-part0.json"
        assert os.path.exists(output_file)

    @pytest.mark.asyncio
    async def test_write_batched_dict_async_generator_list(
        self, base_output_path: str
    ) -> None:
        """Test async generator yielding list[dict]."""
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "test_batched_async_list"),
            chunk_size=100000,
            buffer_size=10,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )

        async def async_gen() -> AsyncGenerator[List[Dict[str, Any]], None]:
            yield [{"id": 1, "name": "test1"}, {"id": 2, "name": "test2"}]
            yield [{"id": 3, "name": "test3"}]

        await json_output.write_batched_dict(async_gen())

        assert json_output.total_record_count == 3
        output_file = f"{json_output.output_path}/chunk-0-part0.json"
        assert os.path.exists(output_file)

    @pytest.mark.asyncio
    async def test_write_batched_dict_invalid_list_contents(
        self, base_output_path: str
    ) -> None:
        """Test write_batched_dict raises TypeError when generator yields invalid list."""
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "test_batched_invalid"),
            chunk_size=100000,
            buffer_size=10,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )

        def sync_gen() -> Generator[List[Any], None, None]:
            yield [1, 2, 3]  # Invalid: list of ints

        with pytest.raises(
            TypeError, match="Expected list\\[dict\\], but list contains non-dict items"
        ):
            await json_output.write_batched_dict(sync_gen())

    @pytest.mark.asyncio
    async def test_write_batched_dict_empty_list_skipped(
        self, base_output_path: str
    ) -> None:
        """Test write_batched_dict skips empty lists from generator."""
        json_output = JsonFileWriter(  # type: ignore
            output_path=os.path.join(base_output_path, "test_batched_empty"),
            chunk_size=100000,
            buffer_size=10,
            typename=None,
            chunk_count=0,
            total_record_count=0,
            chunk_start=None,
        )

        def sync_gen() -> (
            Generator[Union[Dict[str, Any], List[Dict[str, Any]]], None, None]
        ):
            yield []  # Empty list should be skipped
            yield {"id": 1}  # Valid dict should be written
            yield []  # Another empty list should be skipped

        await json_output.write_batched_dict(sync_gen())

        # Only one record should be written
        assert json_output.total_record_count == 1
        output_file = f"{json_output.output_path}/chunk-0-part0.json"
        assert os.path.exists(output_file)
