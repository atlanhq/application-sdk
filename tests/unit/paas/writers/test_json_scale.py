import os
import shutil
import time
from unittest.mock import patch

import pytest

from application_sdk.paas.writers.json import JSONChunkedObjectStoreWriter


class TestJSONChunkedObjectStoreWriterScale:
    @staticmethod
    @pytest.fixture
    def writer():
        shutil.rmtree("/tmp/test", ignore_errors=True)
        with patch.object(
            JSONChunkedObjectStoreWriter, "upload_file", return_value=None
        ):
            writer = JSONChunkedObjectStoreWriter(
                local_file_prefix="/tmp/test/test",
                upload_file_prefix="test",
                chunk_size=20000,
            )
            yield writer

    @staticmethod
    def generate_records(num: int):
        return [{"test": f"test_{i}"} for i in range(num)]

    @classmethod
    async def test_1k(cls, writer):
        await writer.write_list(cls.generate_records(1000))
        await writer.close()
        # 3 files should be created in /tmp/test
        files = os.listdir("/tmp/test")
        assert len(files) == 2
        assert "test-metadata.json" in files
        assert "test-1.json" in files

        with open("/tmp/test/test-metadata.json", "r") as f:
            metadata = f.read()
            assert metadata == '{"total_record_count":1000,"chunk_count":1}\n'

    @classmethod
    async def test_100k(cls, writer):
        start_time = time.time()
        for i in range(50):
            await writer.write_list(cls.generate_records(2000))
        await writer.close()
        end_time = time.time()

        print(f"Time taken: {int(end_time - start_time)} seconds")
        assert end_time - start_time < 5  # should take less than 5 seconds

        files = os.listdir("/tmp/test")
        assert len(files) == 6

        with open("/tmp/test/test-metadata.json", "r") as f:
            metadata = f.read()
            assert metadata == '{"total_record_count":100000,"chunk_count":5}\n'

    @classmethod
    async def test_1m(cls, writer):
        start_time = time.time()
        for i in range(500):
            await writer.write_list(cls.generate_records(2000))
        await writer.close()
        end_time = time.time()

        print(f"Time taken: {int(end_time - start_time)} seconds")
        assert end_time - start_time < 50  # should take less than 50 seconds

        files = os.listdir("/tmp/test")
        assert len(files) == 51

        with open("/tmp/test/test-metadata.json", "r") as f:
            metadata = f.read()
            assert metadata == '{"total_record_count":1000000,"chunk_count":50}\n'
