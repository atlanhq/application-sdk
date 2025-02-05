import os
from unittest.mock import patch

import pandas as pd
import pytest

from application_sdk.outputs.json import JsonOutput


@pytest.fixture
def json_output(tmp_path):
    output_path = str(tmp_path / "test_output")
    json_output = JsonOutput
    return json_output.re_init(
        output_suffix="/tests/raw", output_path=output_path, output_prefix="test_prefix"
    )


@pytest.mark.asyncio
async def test_init(json_output):
    assert json_output.output_path.endswith("/tests/raw")
    assert json_output.output_prefix == "test_prefix"
    assert json_output.chunk_size == 100000
    assert os.path.exists(json_output.output_path)


@pytest.mark.asyncio
async def test_write_dataframe_empty(json_output):
    dataframe = pd.DataFrame()
    await json_output.write_dataframe(dataframe)
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0


@pytest.mark.asyncio
@patch(
    "application_sdk.outputs.objectstore.ObjectStoreOutput.push_file_to_object_store"
)
async def test_write_dataframe_single_chunk(mock_push, json_output):
    dataframe = pd.DataFrame({"col1": range(10), "col2": range(10)})
    await json_output.write_dataframe(dataframe)

    assert json_output.chunk_count == 1
    assert json_output.total_record_count == 10
    assert os.path.exists(f"{json_output.output_path}/1.json")
    mock_push.assert_called_once()


@pytest.mark.asyncio
@patch(
    "application_sdk.outputs.objectstore.ObjectStoreOutput.push_file_to_object_store"
)
async def test_write_dataframe_multiple_chunks(mock_push, json_output):
    json_output.chunk_size = 3
    dataframe = pd.DataFrame({"col1": range(10), "col2": range(10)})
    await json_output.write_dataframe(dataframe)

    # Check if the files are created on the path json_output.output_path
    assert os.path.exists(f"{json_output.output_path}/1.json")
    assert os.path.exists(f"{json_output.output_path}/2.json")
    assert os.path.exists(f"{json_output.output_path}/3.json")
    assert os.path.exists(f"{json_output.output_path}/4.json")

    assert json_output.chunk_count == 4  # 10 rows with chunk_size 3 = 4 chunks
    assert json_output.total_record_count == 10
    assert mock_push.call_count == 4


@pytest.mark.asyncio
async def test_write_dataframe_error(json_output):
    dataframe = "not_a_dataframe"
    await json_output.write_dataframe(dataframe)
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0
