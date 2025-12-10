import os
from unittest.mock import patch

import pytest

from application_sdk.io import DataframeType
from application_sdk.io.json import JsonFileWriter


@pytest.fixture
def temp_output_path(tmp_path):
    return str(tmp_path)


@pytest.mark.asyncio
async def test_write_single_dict(temp_output_path):
    with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
        writer = JsonFileWriter(
            output_path=os.path.join(temp_output_path, "test_single"),
            chunk_size=100,
            dataframe_type=DataframeType.dict,
        )
        data = {"id": 1, "name": "test"}
        await writer.write(data)

        assert writer.total_record_count == 1

        stats = await writer.get_statistics()
        assert stats.total_record_count == 1


@pytest.mark.asyncio
async def test_write_list_of_dicts(temp_output_path):
    with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
        output_path = os.path.join(temp_output_path, "test_list")
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
async def test_write_batches_dicts(temp_output_path):
    with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
        writer = JsonFileWriter(
            output_path=os.path.join(temp_output_path, "test_batches"),
            chunk_size=100,
            dataframe_type=DataframeType.dict,
        )

        def dict_generator():
            yield {"id": 1}
            yield [{"id": 2}, {"id": 3}]

        await writer.write_batches(dict_generator())

        stats = await writer.get_statistics()
        assert stats.total_record_count == 3
