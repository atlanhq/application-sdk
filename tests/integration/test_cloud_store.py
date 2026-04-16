"""Integration tests for CloudStore using a real local object store.

No mocks — exercises the full CloudStore API end-to-end with a local
filesystem-backed obstore store.
"""

from __future__ import annotations

import pytest
from obstore.store import LocalStore

from application_sdk.storage.cloud import CloudStore


@pytest.fixture
def cloud_store(tmp_path):
    """CloudStore backed by a local directory."""
    store_root = tmp_path / "cloud-bucket"
    store_root.mkdir()
    local_store = LocalStore(prefix=str(store_root))
    return CloudStore(local_store, provider="local")


@pytest.fixture
def sample_files(tmp_path):
    """Create sample files for upload tests."""
    src = tmp_path / "source"
    src.mkdir()
    (src / "file1.json").write_text('{"a": 1}')
    (src / "file2.yaml").write_text("key: value")
    sub = src / "subdir"
    sub.mkdir()
    (sub / "nested.json").write_text('{"nested": true}')
    return src


@pytest.mark.integration
class TestCloudStoreIntegration:
    async def test_upload_and_get_bytes(self, cloud_store):
        await cloud_store.upload_bytes("test/hello.txt", b"hello world")
        data = await cloud_store.get_bytes("test/hello.txt")
        assert data == b"hello world"

    async def test_upload_file_and_download(self, cloud_store, tmp_path):
        # Upload
        src = tmp_path / "upload.json"
        src.write_text('{"uploaded": true}')
        size = await cloud_store.upload(src, "data/upload.json")
        assert size > 0

        # Download single file
        dest = tmp_path / "downloaded"
        files = await cloud_store.download(key="data/upload.json", output_dir=dest)
        assert len(files) == 1
        assert files[0].read_text() == '{"uploaded": true}'

    async def test_upload_dir_and_download_prefix(
        self, cloud_store, sample_files, tmp_path
    ):
        # Upload directory
        keys = await cloud_store.upload_dir(sample_files, prefix="import")
        assert len(keys) == 3

        # List
        all_keys = await cloud_store.list(prefix="import")
        assert len(all_keys) == 3

        # Download prefix
        dest = tmp_path / "downloaded"
        files = await cloud_store.download(prefix="import", output_dir=dest)
        assert len(files) == 3

    async def test_list_with_suffix_filter(self, cloud_store, sample_files):
        await cloud_store.upload_dir(sample_files, prefix="mixed")

        json_keys = await cloud_store.list(prefix="mixed", suffix=".json")
        assert len(json_keys) == 2  # file1.json + subdir/nested.json
        assert all(k.endswith(".json") for k in json_keys)

        yaml_keys = await cloud_store.list(prefix="mixed", suffix=".yaml")
        assert len(yaml_keys) == 1

    async def test_download_with_suffix_filter(
        self, cloud_store, sample_files, tmp_path
    ):
        await cloud_store.upload_dir(sample_files, prefix="filtered")

        dest = tmp_path / "filtered"
        files = await cloud_store.download(
            prefix="filtered",
            output_dir=dest,
            suffix_filter={".json"},
        )
        assert len(files) == 2  # only .json files

    async def test_get_bytes_not_found(self, cloud_store):
        from application_sdk.storage.errors import StorageNotFoundError

        with pytest.raises(StorageNotFoundError):
            await cloud_store.get_bytes("nonexistent/key.txt")

    async def test_download_empty_prefix_raises(self, cloud_store, tmp_path):
        from application_sdk.storage.errors import StorageError

        with pytest.raises(StorageError):
            await cloud_store.download(
                prefix="empty-prefix", output_dir=tmp_path / "empty"
            )

    async def test_list_empty(self, cloud_store):
        keys = await cloud_store.list(prefix="nothing-here")
        assert keys == []

    async def test_upload_bytes_roundtrip(self, cloud_store, tmp_path):
        content = b'{"roundtrip": true}'
        await cloud_store.upload_bytes("rt/data.json", content)

        dest = tmp_path / "rt"
        files = await cloud_store.download(key="rt/data.json", output_dir=dest)
        assert files[0].read_bytes() == content

    async def test_provider_property(self, cloud_store):
        assert cloud_store.provider == "local"

    async def test_store_property(self, cloud_store):
        assert cloud_store.store is not None
