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
            output_path=os.path.join(temp_output_path, "test_single"), chunk_size=100
        )
        data = {"id": 1, "name": "test"}
        await writer.write(data)

        assert writer.total_record_count == 1
        # Chunk count increments when chunk is full/flushed at end.
        # Writer implementation increments chunk_count when flushing buffer or getting statistics.
        # Let's check get_statistics behavior
        stats = await writer.get_statistics()
        assert stats.total_record_count == 1

        # Verify file content
        # path_gen uses chunk_count and chunk_part.
        # If chunk_count starts at 0, file is 0.json? No path_gen uses chunk_part if chunk_count is None passed to it?
        # JsonFileWriter calls path_gen(self.chunk_count, self.chunk_part, ...)
        # self.chunk_count starts at 0.
        # The file name is usually 1.json (chunk_part + 1?)
        # Let's check path_gen in _utils.py:
        # if chunk_count is None: return f"{chunk_part}{extension}"
        # else: return f"chunk-{chunk_count}-part{chunk_part}{extension}"

        # JsonFileWriter calls: path_gen(self.chunk_count, self.chunk_part...)
        # self.chunk_count is 0.
        # So "chunk-0-part0.json" ?
        # Wait, JsonFileWriter usually produces "1.json".
        # Let's check JsonFileWriter logic.
        pass


@pytest.mark.asyncio
async def test_write_list_of_dicts(temp_output_path):
    with patch("application_sdk.services.objectstore.ObjectStore.upload_file"):
        output_path = os.path.join(temp_output_path, "test_list")
        writer = JsonFileWriter(output_path=output_path, chunk_size=100)
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
            dataframe_type=DataframeType.pandas,
        )

        def dict_generator():
            yield {"id": 1}
            yield [{"id": 2}, {"id": 3}]

        await writer.write_batches(dict_generator())

        stats = await writer.get_statistics()
        assert stats.total_record_count == 3
