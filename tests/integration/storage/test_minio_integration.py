"""Integration tests for obstore-based storage against MinIO (real S3 protocol).

Requires MinIO running locally:
    docker run -d --name minio-test -p 9000:9000 -p 9001:9001 \
        -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
        minio/minio server /data --console-address ":9001"

    # Create bucket:
    docker run --rm --network host --entrypoint sh minio/mc -c \
        "mc alias set local http://localhost:9000 minioadmin minioadmin && mc mb local/test-bucket"

Run:
    AWS_ENDPOINT_URL=http://localhost:9000 \
    AWS_ACCESS_KEY_ID=minioadmin \
    AWS_SECRET_ACCESS_KEY=minioadmin \
    AWS_DEFAULT_REGION=us-east-1 \
    AWS_ALLOW_HTTP=true \
    ATLAN_OBJECT_STORE_BUCKET=test-bucket \
    uv run pytest tests/integration/storage/test_minio_integration.py -v
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

from application_sdk.services.storage import ObjectStore
from application_sdk.services.storage._registry import StoreRegistry

# Skip entire module if MinIO env vars not set
pytestmark = pytest.mark.skipif(
    not os.getenv("AWS_ENDPOINT_URL"),
    reason="MinIO not configured (set AWS_ENDPOINT_URL)",
)


@pytest.fixture(autouse=True)
def reset_registry():
    StoreRegistry.reset()
    yield
    StoreRegistry.reset()


@pytest.fixture
def unique_prefix():
    """Generate a unique prefix to avoid test interference."""
    return f"test-{uuid.uuid4().hex[:8]}"


class TestMinIOIntegration:
    """End-to-end tests against real S3 protocol via MinIO."""

    async def test_upload_download_round_trip(self, unique_prefix):
        """Upload bytes, download, verify content matches."""
        key = f"{unique_prefix}/hello.txt"
        data = b"hello from obstore integration test"

        await ObjectStore.upload_file_from_bytes(data, key, store_name="objectstore")

        content = await ObjectStore.get_content(key, store_name="objectstore")
        assert content == data

        # Cleanup
        await ObjectStore.delete_file(key, store_name="objectstore")

    async def test_upload_file_and_download(self, unique_prefix, tmp_path):
        """Upload a local file, download to different path, verify."""
        # Create source file
        source = tmp_path / "source.txt"
        source.write_text("integration test content")

        key = f"{unique_prefix}/uploaded.txt"
        dest = tmp_path / "downloaded.txt"

        await ObjectStore.upload_file(
            str(source), key, store_name="objectstore", retain_local_copy=True
        )
        await ObjectStore.download_file(key, str(dest), store_name="objectstore")

        assert dest.read_text() == "integration test content"

        # Cleanup
        await ObjectStore.delete_file(key, store_name="objectstore")

    async def test_list_files(self, unique_prefix):
        """Upload multiple files, list them, verify count."""
        keys = [f"{unique_prefix}/a.txt", f"{unique_prefix}/b.txt", f"{unique_prefix}/sub/c.txt"]
        for k in keys:
            await ObjectStore.upload_file_from_bytes(b"data", k, store_name="objectstore")

        files = await ObjectStore.list_files(unique_prefix, store_name="objectstore")
        assert len(files) == 3

        # Cleanup
        await ObjectStore.delete_prefix(unique_prefix, store_name="objectstore")

    async def test_exists(self, unique_prefix):
        """Check exists returns True/False correctly."""
        key = f"{unique_prefix}/exists-test.txt"

        assert not await ObjectStore.exists(key, store_name="objectstore")

        await ObjectStore.upload_file_from_bytes(b"x", key, store_name="objectstore")
        assert await ObjectStore.exists(key, store_name="objectstore")

        await ObjectStore.delete_file(key, store_name="objectstore")
        assert not await ObjectStore.exists(key, store_name="objectstore")

    async def test_delete_prefix(self, unique_prefix):
        """Upload files under prefix, delete prefix, verify all gone."""
        keys = [f"{unique_prefix}/del/1.txt", f"{unique_prefix}/del/2.txt"]
        for k in keys:
            await ObjectStore.upload_file_from_bytes(b"data", k, store_name="objectstore")

        await ObjectStore.delete_prefix(f"{unique_prefix}/del", store_name="objectstore")

        files = await ObjectStore.list_files(f"{unique_prefix}/del", store_name="objectstore")
        assert len(files) == 0

    async def test_upload_prefix_and_download_prefix(self, unique_prefix, tmp_path):
        """Upload a directory, download to different location, verify structure."""
        # Create local directory structure
        src_dir = tmp_path / "upload_src"
        src_dir.mkdir()
        (src_dir / "file1.txt").write_text("content1")
        sub = src_dir / "subdir"
        sub.mkdir()
        (sub / "file2.txt").write_text("content2")

        dest_key = f"{unique_prefix}/dir-test"
        await ObjectStore.upload_prefix(
            str(src_dir), dest_key, store_name="objectstore", retain_local_copy=True
        )

        # Download to new location
        dl_dir = tmp_path / "download_dest"
        dl_dir.mkdir()
        await ObjectStore.download_prefix(dest_key, str(dl_dir), store_name="objectstore")

        assert (dl_dir / "file1.txt").read_text() == "content1"
        assert (dl_dir / "subdir" / "file2.txt").read_text() == "content2"

        # Cleanup
        await ObjectStore.delete_prefix(dest_key, store_name="objectstore")

    async def test_large_file_upload(self, unique_prefix, tmp_path):
        """Upload a file larger than small threshold to test streaming path.

        Uses 1MB — not truly large, but exercises the upload_file code path.
        For actual 64MB+ streaming tests, increase size (slower).
        """
        source = tmp_path / "large.bin"
        data = os.urandom(1 * 1024 * 1024)  # 1MB
        source.write_bytes(data)

        key = f"{unique_prefix}/large.bin"
        await ObjectStore.upload_file(
            str(source), key, store_name="objectstore", retain_local_copy=True
        )

        content = await ObjectStore.get_content(key, store_name="objectstore")
        assert content == data

        # Cleanup
        await ObjectStore.delete_file(key, store_name="objectstore")

    async def test_get_content_suppress_error_missing_key(self, unique_prefix):
        """get_content with suppress_error=True returns None for missing key."""
        result = await ObjectStore.get_content(
            f"{unique_prefix}/nonexistent.txt",
            store_name="objectstore",
            suppress_error=True,
        )
        assert result is None

    async def test_get_content_raises_for_missing_key(self, unique_prefix):
        """get_content without suppress_error raises for missing key."""
        with pytest.raises(Exception):
            await ObjectStore.get_content(
                f"{unique_prefix}/nonexistent.txt", store_name="objectstore"
            )
