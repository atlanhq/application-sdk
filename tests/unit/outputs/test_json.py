import os
from unittest.mock import patch

import pandas as pd
import pytest

from application_sdk.outputs.json import JsonOutput


@pytest.fixture
def json_output(tmp_path):
    output_path = str(tmp_path / "test_output")
    return JsonOutput(output_path=output_path, upload_file_prefix="test_prefix")


@pytest.mark.asyncio
async def test_init(json_output):
    assert json_output.output_path.endswith("test_output")
    assert json_output.upload_file_prefix == "test_prefix"
    assert json_output.chunk_size == 30000
    assert os.path.exists(json_output.output_path)


@pytest.mark.asyncio
async def test_write_df_empty(json_output):
    df = pd.DataFrame()
    await json_output.write_batched_df(df)
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0


@pytest.mark.asyncio
@patch("application_sdk.inputs.objectstore.ObjectStore.push_file_to_object_store")
async def test_write_df_single_chunk(mock_push, json_output):
    df = pd.DataFrame({"col1": range(10), "col2": range(10)})
    await json_output.write_batched_df(df)

    assert json_output.chunk_count == 1
    assert json_output.total_record_count == 10
    assert os.path.exists(f"{json_output.output_path}/1.json")
    mock_push.assert_called_once()


@pytest.mark.asyncio
@patch("application_sdk.inputs.objectstore.ObjectStore.push_file_to_object_store")
async def test_write_df_multiple_chunks(mock_push, json_output):
    json_output.chunk_size = 3
    df = pd.DataFrame({"col1": range(10), "col2": range(10)})
    await json_output.write_batched_df(df)

    assert json_output.chunk_count == 4  # 10 rows with chunk_size 3 = 4 chunks
    assert json_output.total_record_count == 10
    assert mock_push.call_count == 4


@pytest.mark.asyncio
async def test_write_df_error(json_output):
    df = "not_a_dataframe"
    await json_output.write_batched_df(df)
    assert json_output.chunk_count == 0
    assert json_output.total_record_count == 0
