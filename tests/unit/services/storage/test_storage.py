"""Unit tests for the obstore-based storage module.

Uses obstore's LocalStore with tmp_path — real filesystem I/O, no mocking.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from application_sdk.services.storage._config import (
    StorageBindingConfig,
    create_store,
    parse_binding_dict,
    resolve_config_from_env,
)
from application_sdk.services.storage._errors import (
    StorageConfigError,
    StorageError,
    StorageNotFoundError,
    StorageOperationError,
    StoragePermissionError,
)
from application_sdk.services.storage._ops import (
    delete,
    exists,
    get_bytes,
    head,
    list_paths,
    put,
    stream_download,
    stream_upload,
)
from application_sdk.services.storage._registry import StoreRegistry

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def local_store(tmp_path):
    """Create an obstore LocalStore backed by a temp directory."""
    from obstore.store import LocalStore

    store_root = tmp_path / "store"
    store_root.mkdir()
    return LocalStore(prefix=str(store_root))


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset store registry between tests."""
    StoreRegistry.reset()
    yield
    StoreRegistry.reset()


# =============================================================================
# Error Hierarchy Tests
# =============================================================================


class TestErrors:
    def test_storage_error_base(self):
        err = StorageError(
            "test error", store_type="s3", path="my/key", operation="get"
        )
        assert "test error" in str(err)
        assert "store_type=s3" in str(err)
        assert "path=my/key" in str(err)
        assert err.error_code == "STORAGE_OPERATION"

    def test_storage_not_found_error(self):
        err = StorageNotFoundError(path="missing/key")
        assert err.error_code == "STORAGE_NOT_FOUND"
        assert "missing/key" in str(err)

    def test_storage_permission_error(self):
        err = StoragePermissionError(path="secret/key")
        assert err.error_code == "STORAGE_PERMISSION"

    def test_storage_config_error_with_binding(self):
        err = StorageConfigError(
            "bad config", binding_name="my-binding", config_file="/tmp/test.yaml"
        )
        assert err.error_code == "STORAGE_CONFIG"
        assert "binding=my-binding" in str(err)
        assert "file=/tmp/test.yaml" in str(err)

    def test_storage_operation_error(self):
        err = StorageOperationError("op failed", operation="upload")
        assert err.error_code == "STORAGE_OPERATION"

    def test_error_with_cause(self):
        cause = ValueError("root cause")
        err = StorageError("wrapper", cause=cause)
        assert "caused_by=ValueError: root cause" in str(err)

    def test_error_inheritance(self):
        assert issubclass(StorageNotFoundError, StorageError)
        assert issubclass(StoragePermissionError, StorageError)
        assert issubclass(StorageConfigError, StorageError)
        assert issubclass(StorageOperationError, StorageError)


# =============================================================================
# Config Tests
# =============================================================================


class TestConfig:
    def test_parse_s3_binding(self):
        spec = {
            "type": "bindings.aws.s3",
            "metadata": [
                {"name": "bucket", "value": "my-bucket"},
                {"name": "region", "value": "us-west-2"},
                {"name": "accessKey", "value": "AKID"},
                {"name": "secretKey", "value": "SECRET"},
            ],
        }
        config = parse_binding_dict(spec, name="test-s3")
        assert config.provider == "s3"
        assert config.bucket == "my-bucket"
        assert config.config["region"] == "us-west-2"
        assert config.config["access_key_id"] == "AKID"

    def test_parse_azure_binding(self):
        spec = {
            "type": "bindings.azure.blobstorage",
            "metadata": [
                {"name": "containerName", "value": "my-container"},
                {"name": "accountName", "value": "myaccount"},
                {"name": "accountKey", "value": "mykey"},
            ],
        }
        config = parse_binding_dict(spec, name="test-azure")
        assert config.provider == "azure"
        assert config.bucket == "my-container"
        assert config.config["account_name"] == "myaccount"

    def test_parse_local_binding(self):
        spec = {
            "type": "bindings.local.storage",
            "metadata": [{"name": "rootPath", "value": "/tmp/store"}],
        }
        config = parse_binding_dict(spec, name="test-local")
        assert config.provider == "local"
        assert config.bucket == "/tmp/store"

    def test_parse_unsupported_binding_raises(self):
        spec = {"type": "bindings.unknown.store", "metadata": []}
        with pytest.raises(StorageConfigError, match="Unsupported binding type"):
            parse_binding_dict(spec)

    def test_parse_s3_missing_bucket_raises(self):
        spec = {
            "type": "bindings.aws.s3",
            "metadata": [{"name": "region", "value": "us-east-1"}],
        }
        with pytest.raises(StorageConfigError, match="missing required 'bucket'"):
            parse_binding_dict(spec)

    def test_env_var_auto_detection_local(self):
        with patch.dict(os.environ, {"ATLAN_OBJECT_STORE_BACKEND": "local"}):
            config = resolve_config_from_env("test-store")
            assert config is not None
            assert config.provider == "local"

    def test_env_var_auto_detection_azure(self):
        env = {
            "AZURE_STORAGE_ACCOUNT": "myaccount",
            "AZURE_STORAGE_ACCESS_KEY": "mykey",
            "ATLAN_OBJECT_STORE_BUCKET": "mybucket",
        }
        with patch.dict(os.environ, env, clear=False):
            config = resolve_config_from_env("test-store")
            assert config is not None
            assert config.provider == "azure"
            assert config.config["account_name"] == "myaccount"

    def test_env_var_auto_detection_s3_default(self):
        with patch.dict(os.environ, {}, clear=False):
            config = resolve_config_from_env("test-store")
            assert config is not None
            assert config.provider == "s3"

    def test_create_local_store(self, tmp_path):
        config = StorageBindingConfig(
            name="test", provider="local", bucket=str(tmp_path)
        )
        store = create_store(config)
        assert store is not None

    def test_create_store_unknown_provider_raises(self):
        config = StorageBindingConfig(name="test", provider="unknown", bucket="bucket")
        with pytest.raises(StorageConfigError, match="Unknown storage provider"):
            create_store(config)


# =============================================================================
# Registry Tests
# =============================================================================


class TestRegistry:
    async def test_get_store_creates_and_caches(self, tmp_path):
        with patch(
            "application_sdk.services.storage._registry.resolve_store_config",
            return_value=StorageBindingConfig(
                name="test", provider="local", bucket=str(tmp_path)
            ),
        ):
            store1 = await StoreRegistry.get_store("test-store")
            store2 = await StoreRegistry.get_store("test-store")
            assert store1 is store2

    async def test_different_names_different_instances(self, tmp_path):
        dir1 = tmp_path / "store1"
        dir2 = tmp_path / "store2"
        dir1.mkdir()
        dir2.mkdir()

        def mock_resolve(name):
            path = str(dir1) if name == "store-a" else str(dir2)
            return StorageBindingConfig(name=name, provider="local", bucket=path)

        with patch(
            "application_sdk.services.storage._registry.resolve_store_config",
            side_effect=mock_resolve,
        ):
            store_a = await StoreRegistry.get_store("store-a")
            store_b = await StoreRegistry.get_store("store-b")
            assert store_a is not store_b

    async def test_reset_clears_cache(self, tmp_path):
        with patch(
            "application_sdk.services.storage._registry.resolve_store_config",
            return_value=StorageBindingConfig(
                name="test", provider="local", bucket=str(tmp_path)
            ),
        ):
            await StoreRegistry.get_store("test-store")
            assert "test-store" in StoreRegistry._stores
            StoreRegistry.reset()
            assert "test-store" not in StoreRegistry._stores


# =============================================================================
# Core Operations Tests (real I/O with LocalStore)
# =============================================================================


class TestOps:
    async def test_put_and_get_bytes(self, local_store):
        await put(local_store, "test/file.txt", b"hello world")
        data = await get_bytes(local_store, "test/file.txt")
        assert data == b"hello world"

    async def test_get_bytes_not_found_raises(self, local_store):
        with pytest.raises(Exception):
            await get_bytes(local_store, "nonexistent/key")

    async def test_delete(self, local_store):
        await put(local_store, "test/to-delete.txt", b"delete me")
        assert await exists(local_store, "test/to-delete.txt")
        await delete(local_store, "test/to-delete.txt")
        assert not await exists(local_store, "test/to-delete.txt")

    async def test_exists_true(self, local_store):
        await put(local_store, "test/exists.txt", b"data")
        assert await exists(local_store, "test/exists.txt") is True

    async def test_exists_false(self, local_store):
        assert await exists(local_store, "test/nope.txt") is False

    async def test_head(self, local_store):
        await put(local_store, "test/meta.txt", b"twelve bytes")
        meta = await head(local_store, "test/meta.txt")
        assert meta["path"] == "test/meta.txt"
        assert meta["size"] == 12

    async def test_list_paths(self, local_store):
        await put(local_store, "prefix/a.txt", b"a")
        await put(local_store, "prefix/b.txt", b"b")
        await put(local_store, "other/c.txt", b"c")

        paths = await list_paths(local_store, "prefix/")
        assert len(paths) == 2
        assert "prefix/a.txt" in paths
        assert "prefix/b.txt" in paths

    async def test_list_paths_empty(self, local_store):
        paths = await list_paths(local_store, "empty/")
        assert paths == []

    async def test_stream_upload_and_download(self, local_store, tmp_path):
        # Create a test file
        source = tmp_path / "upload.bin"
        content = b"x" * 1024
        source.write_bytes(content)

        # Upload
        bytes_up = await stream_upload(
            local_store, source, "streamed/file.bin", chunk_size=256
        )
        assert bytes_up == 1024

        # Download
        dest = tmp_path / "download.bin"
        bytes_down = await stream_download(local_store, "streamed/file.bin", dest)
        assert bytes_down == 1024
        assert dest.read_bytes() == content

    async def test_stream_download_not_found(self, local_store, tmp_path):
        dest = tmp_path / "missing.bin"
        with pytest.raises(FileNotFoundError):
            await stream_download(local_store, "nonexistent/file.bin", dest)

    async def test_stream_download_creates_parent_dirs(self, local_store, tmp_path):
        await put(local_store, "deep/nested/file.txt", b"nested")
        dest = tmp_path / "new" / "deep" / "nested" / "file.txt"
        await stream_download(local_store, "deep/nested/file.txt", dest)
        assert dest.read_bytes() == b"nested"


# =============================================================================
# ObjectStore Facade Tests (real I/O with LocalStore)
# =============================================================================


class TestObjectStoreFacade:
    """Test the ObjectStore facade using a real LocalStore."""

    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path):
        """Set up a LocalStore and patch the registry to use it."""
        self.store_root = tmp_path / "facade_store"
        self.store_root.mkdir()
        self.tmp_path = tmp_path

        from obstore.store import LocalStore

        self.store = LocalStore(prefix=str(self.store_root))
        self._config = StorageBindingConfig(
            name="objectstore", provider="local", bucket=str(self.store_root)
        )

        self._patcher = patch(
            "application_sdk.services.storage._registry.resolve_store_config",
            return_value=self._config,
        )
        self._patcher.start()
        yield
        self._patcher.stop()

    async def test_upload_and_download_file(self):
        from application_sdk.services.storage import ObjectStore

        # Create source file
        source = self.tmp_path / "src.txt"
        source.write_text("hello facade")

        await ObjectStore.upload_file(
            source=str(source),
            destination="facade/test.txt",
            retain_local_copy=True,
        )

        dest = self.tmp_path / "dst.txt"
        await ObjectStore.download_file(
            source="facade/test.txt",
            destination=str(dest),
        )
        assert dest.read_text() == "hello facade"

    async def test_upload_file_deletes_source_by_default(self):
        from application_sdk.services.storage import ObjectStore

        source = self.tmp_path / "ephemeral.txt"
        source.write_text("will be deleted")

        await ObjectStore.upload_file(
            source=str(source),
            destination="facade/ephemeral.txt",
            retain_local_copy=False,
        )
        assert not source.exists()

    async def test_upload_file_retains_source_when_flag_set(self):
        from application_sdk.services.storage import ObjectStore

        source = self.tmp_path / "keep.txt"
        source.write_text("will be kept")

        await ObjectStore.upload_file(
            source=str(source),
            destination="facade/keep.txt",
            retain_local_copy=True,
        )
        assert source.exists()

    async def test_upload_file_from_bytes(self):
        from application_sdk.services.storage import ObjectStore

        await ObjectStore.upload_file_from_bytes(
            file_content=b"bytes content",
            destination="facade/bytes.txt",
        )
        data = await ObjectStore.get_content("facade/bytes.txt")
        assert data == b"bytes content"

    async def test_get_content_suppress_error(self):
        from application_sdk.services.storage import ObjectStore

        result = await ObjectStore.get_content("nonexistent/key", suppress_error=True)
        assert result is None

    async def test_get_content_raises_without_suppress(self):
        from application_sdk.services.storage import ObjectStore

        with pytest.raises(Exception):
            await ObjectStore.get_content("nonexistent/key", suppress_error=False)

    async def test_exists_true_and_false(self):
        from application_sdk.services.storage import ObjectStore

        source = self.tmp_path / "exist_test.txt"
        source.write_text("exists")
        await ObjectStore.upload_file(
            str(source), "facade/exist_test.txt", retain_local_copy=True
        )

        assert await ObjectStore.exists("facade/exist_test.txt") is True
        assert await ObjectStore.exists("facade/nope.txt") is False

    async def test_delete_file(self):
        from application_sdk.services.storage import ObjectStore

        source = self.tmp_path / "del.txt"
        source.write_text("delete me")
        await ObjectStore.upload_file(
            str(source), "facade/del.txt", retain_local_copy=True
        )

        assert await ObjectStore.exists("facade/del.txt")
        await ObjectStore.delete_file("facade/del.txt")
        assert not await ObjectStore.exists("facade/del.txt")

    async def test_list_files(self):
        from application_sdk.services.storage import ObjectStore

        for name in ("a.txt", "b.txt", "c.txt"):
            src = self.tmp_path / name
            src.write_text(name)
            await ObjectStore.upload_file(
                str(src), f"listing/{name}", retain_local_copy=True
            )

        files = await ObjectStore.list_files("listing")
        assert len(files) == 3

    async def test_upload_and_download_prefix(self):
        from application_sdk.services.storage import ObjectStore

        # Create source directory with files
        src_dir = self.tmp_path / "upload_dir"
        src_dir.mkdir()
        (src_dir / "file1.txt").write_text("one")
        sub = src_dir / "sub"
        sub.mkdir()
        (sub / "file2.txt").write_text("two")

        await ObjectStore.upload_prefix(
            source=str(src_dir),
            destination="prefix_test",
            retain_local_copy=True,
        )

        # Download prefix
        dl_dir = self.tmp_path / "download_dir"
        dl_dir.mkdir()
        await ObjectStore.download_prefix(
            source="prefix_test",
            destination=str(dl_dir),
        )

        # Verify files exist
        downloaded_files = list(dl_dir.rglob("*"))
        downloaded_files = [f for f in downloaded_files if f.is_file()]
        assert len(downloaded_files) == 2

    async def test_delete_prefix(self):
        from application_sdk.services.storage import ObjectStore

        for name in ("x.txt", "y.txt"):
            src = self.tmp_path / name
            src.write_text(name)
            await ObjectStore.upload_file(
                str(src), f"to_delete/{name}", retain_local_copy=True
            )

        files = await ObjectStore.list_files("to_delete")
        assert len(files) == 2

        await ObjectStore.delete_prefix("to_delete")
        files = await ObjectStore.list_files("to_delete")
        assert len(files) == 0


# =============================================================================
# as_store_key Tests
# =============================================================================


class TestAsStoreKey:
    def test_empty_string(self):
        from application_sdk.services.storage import ObjectStore

        assert ObjectStore.as_store_key("") == ""

    def test_relative_key_unchanged(self):
        from application_sdk.services.storage import ObjectStore

        assert (
            ObjectStore.as_store_key("artifacts/data/file.txt")
            == "artifacts/data/file.txt"
        )

    def test_strips_leading_slash(self):
        from application_sdk.services.storage import ObjectStore

        assert ObjectStore.as_store_key("/artifacts/file.txt") == "artifacts/file.txt"

    def test_strips_trailing_slash(self):
        from application_sdk.services.storage import ObjectStore

        assert ObjectStore.as_store_key("artifacts/data/") == "artifacts/data"

    def test_normalizes_backslashes(self):
        from application_sdk.services.storage import ObjectStore

        result = ObjectStore.as_store_key("artifacts\\data\\file.txt")
        assert "\\" not in result
        assert "file.txt" in result
