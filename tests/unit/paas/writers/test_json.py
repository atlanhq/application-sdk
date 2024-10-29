import os
import shutil

# from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from application_sdk.paas.writers.json import JSONChunkedObjectStoreWriter

# from hypothesis import given, settings
# from hypothesis import strategies as st


class TestJSONChunkedObjectStoreWriter:
    @staticmethod
    @pytest.fixture()
    def writer():
        shutil.rmtree("/tmp/test", ignore_errors=True)

        with patch.object(
            JSONChunkedObjectStoreWriter, "upload_file", return_value=None
        ):
            writer = JSONChunkedObjectStoreWriter(
                local_file_prefix="/tmp/test/test",
                upload_file_prefix="test",
                chunk_size=30000,
                buffer_size=1024,
            )
            yield writer

    @staticmethod
    async def test_write(writer):
        await writer.write({"test": "test"})
        assert writer.current_record_count == 1
        assert writer.total_record_count == 1

    # @staticmethod
    # @given(st.dictionaries(st.text(), st.text()))
    # @settings(max_examples=10)
    # async def test_write_hypothesis(data: Dict[str, Any]):
    #     async with JSONChunkedObjectStoreWriter(
    #         local_file_prefix="/tmp/test/test",
    #         upload_file_prefix="test",
    #         chunk_size=2,
    #         buffer_size=1024,
    #     ) as w:
    #         await w.write(data)
    #         assert w.current_record_count == 1
    #         assert w.total_record_count == 1

    @staticmethod
    async def test_write_list(writer):
        await writer.write_list([{"test": "test"}, {"test": "test"}])
        assert writer.current_record_count == 2
        assert writer.total_record_count == 2

    # @staticmethod
    # @given(st.lists(st.dictionaries(st.text(), st.text())))
    # @settings(max_examples=10)
    # async def test_write_list_hypothesis(data: List[Dict[str, Any]]):
    #     async with JSONChunkedObjectStoreWriter(
    #         local_file_prefix="/tmp/test/test",
    #         upload_file_prefix="test",
    #         chunk_size=2,
    #         buffer_size=1024,
    #     ) as w:
    #         await w.write_list(data)
    #         assert w.total_record_count == len(data)

    @staticmethod
    async def test_close(writer):
        await writer.write_list(
            [
                {"test": "test"},
                {"test": "test"},
                {"test": "test"},
                {"test": "test"},
            ]
        )
        await writer.close()
        # 3 files should be created in /tmp/test
        files = os.listdir("/tmp/test")
        assert len(files) == 3
        assert "test-metadata.json" in files
        assert "test-1.json" in files
        assert "test-2.json" in files

        with open("/tmp/test/test-metadata.json", "r") as f:
            metadata = f.read()
            assert metadata == '{"total_record_count":4,"chunk_count":2}\n'

        with open("/tmp/test/test-1.json", "r") as f:
            data = f.read()
            assert data == '{"test":"test"}\n{"test":"test"}\n'

        with open("/tmp/test/test-2.json", "r") as f:
            data = f.read()
            assert data == '{"test":"test"}\n{"test":"test"}\n'
