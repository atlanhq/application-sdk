import asyncio
import os
import shutil
from unittest.mock import patch

import pytest

from application_sdk.paas.readers.json import JSONChunkedObjectStoreReader

# Assuming the JSONChunkedObjectStoreReader class is imported
# from your_module import JSONChunkedObjectStoreReader


class TestJSONChunkedObjectStoreReader:
    @staticmethod
    @pytest.fixture(scope="module")
    def setup_files():
        """Setup temporary JSON files for testing."""
        # Create a temporary directory
        temp_dir = "/tmp/test_reader"
        os.makedirs(temp_dir, exist_ok=True)

        # Create example chunk files
        with open(os.path.join(temp_dir, "test-0.json"), "w") as f:
            f.write('{"key1": "value1"}\n{"key2": "value2"}\n')
        with open(os.path.join(temp_dir, "test-1.json"), "w") as f:
            f.write('{"key3": "value3"}\n{"key4": "value4"}\n')

        yield temp_dir

        # Cleanup
        shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    @pytest.fixture()
    def reader(setup_files):
        """Fixture for JSONChunkedObjectStoreReader."""
        with patch.object(
            JSONChunkedObjectStoreReader, "download_file", return_value=None
        ):
            reader = JSONChunkedObjectStoreReader(
                local_file_path=setup_files,
                download_file_prefix="/tmp",
                typename="test",
            )
            reader.lock = asyncio.Lock()  # Mock lock for async usage
            yield reader

    @staticmethod
    async def test_read_chunk(reader):
        """Test reading the first chunk."""
        data = await reader.read_chunk("test-0.json")
        assert data == [{"key1": "value1"}, {"key2": "value2"}]

    @staticmethod
    async def test_read_second_chunk(reader):
        """Test reading the second chunk."""
        data = await reader.read_chunk("test-1.json")
        assert data == [{"key3": "value3"}, {"key4": "value4"}]

    @staticmethod
    async def test_read_empty_chunk_file(reader):
        """Test reading an empty chunk file."""
        # Create an empty file for testing
        with open(os.path.join(reader.local_file_path, "test-2.json"), "w") as f:
            f.write("")

        data = await reader.read_chunk("test-2.json")
        assert data == []  # Expecting an empty list for an empty chunk file
