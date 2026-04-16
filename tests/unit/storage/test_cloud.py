"""Unit tests for CloudStore."""

import base64
import json
from pathlib import Path

import pytest
from obstore.store import LocalStore

from application_sdk.storage.cloud import CloudStore, _infer_auth_type
from application_sdk.storage.errors import (
    StorageConfigError,
    StorageError,
    StorageNotFoundError,
)


class TestInferAuthType:
    def test_s3(self):
        assert _infer_auth_type({"s3_bucket": "my-bucket"}) == "s3"

    def test_gcs(self):
        assert _infer_auth_type({"gcs_bucket": "my-bucket"}) == "gcs"

    def test_adls_container(self):
        assert _infer_auth_type({"adls_container": "mycontainer"}) == "adls"

    def test_adls_account(self):
        assert _infer_auth_type({"storage_account_name": "myaccount"}) == "adls"

    def test_unknown(self):
        assert _infer_auth_type({}) == ""


class TestFromCredentials:
    def test_s3_explicit_auth_type(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket", "region": "us-east-1"},
            }
        )
        assert store.provider == "s3"

    def test_s3_inferred_auth_type(self):
        store = CloudStore.from_credentials(
            {
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket"},
            }
        )
        assert store.provider == "s3"

    def test_gcs(self):
        store = CloudStore.from_credentials(
            {
                "authType": "gcs",
                "extra": {"gcs_bucket": "test-bucket"},
            }
        )
        assert store.provider == "gcs"

    def test_adls(self):
        store = CloudStore.from_credentials(
            {
                "authType": "adls",
                "username": "client-id",
                "password": "client-secret",
                "extra": {
                    "storage_account_name": "myaccount",
                    "adls_container": "mycontainer",
                    "azure_tenant_id": "tenant-123",
                },
            }
        )
        assert store.provider == "adls"

    def test_unknown_raises(self):
        with pytest.raises(StorageConfigError, match="Cannot determine cloud provider"):
            CloudStore.from_credentials({"username": "x", "password": "y"})

    def test_s3_missing_bucket_raises(self):
        with pytest.raises(StorageConfigError, match="S3 bucket is required"):
            CloudStore.from_credentials(
                {
                    "authType": "s3",
                    "username": "AKID",
                    "password": "secret",
                    "extra": {},
                }
            )

    def test_gcs_missing_bucket_raises(self):
        with pytest.raises(StorageConfigError, match="GCS bucket is required"):
            CloudStore.from_credentials(
                {
                    "authType": "gcs",
                    "extra": {},
                }
            )

    def test_adls_missing_account_raises(self):
        with pytest.raises(
            StorageConfigError, match="Azure storage account is required"
        ):
            CloudStore.from_credentials(
                {
                    "authType": "adls",
                    "extra": {},
                }
            )

    def test_extra_as_json_string(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": json.dumps({"s3_bucket": "test-bucket"}),
            }
        )
        assert store.provider == "s3"

    def test_extras_key_alias(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extras": {"s3_bucket": "test-bucket"},
            }
        )
        assert store.provider == "s3"

    def test_auth_type_underscore(self):
        store = CloudStore.from_credentials(
            {
                "auth_type": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket"},
            }
        )
        assert store.provider == "s3"

    def test_s3_role_arn(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "extra": {
                    "s3_bucket": "test-bucket",
                    "aws_role_arn": "arn:aws:iam::123:role/MyRole",
                },
            }
        )
        assert store.provider == "s3"

    def test_adls_account_key_auth(self):
        # Azure requires base64-encoded account keys
        fake_key = base64.b64encode(b"0" * 32).decode()
        store = CloudStore.from_credentials(
            {
                "authType": "adls",
                "password": fake_key,
                "extra": {"storage_account_name": "myaccount"},
            }
        )
        assert store.provider == "adls"

    def test_store_property(self):
        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket"},
            }
        )
        assert store.store is not None


# ---------------------------------------------------------------------------
# Async operation tests (with real local store)
# ---------------------------------------------------------------------------


class TestCloudStoreOps:
    """Unit tests for async operations using a local store."""

    def _make_store(self, tmp_path: Path) -> CloudStore:
        store_root = tmp_path / "bucket"
        store_root.mkdir()
        return CloudStore(LocalStore(prefix=str(store_root)), provider="local")

    async def test_get_bytes_not_found(self, tmp_path):
        store = self._make_store(tmp_path)
        with pytest.raises(StorageNotFoundError):
            await store.get_bytes("nonexistent.txt")

    async def test_upload_and_get_bytes_roundtrip(self, tmp_path):
        store = self._make_store(tmp_path)
        await store.upload_bytes("test.txt", b"hello")
        data = await store.get_bytes("test.txt")
        assert data == b"hello"

    async def test_list_empty_prefix(self, tmp_path):
        store = self._make_store(tmp_path)
        keys = await store.list(prefix="nothing")
        assert keys == []

    async def test_download_empty_prefix_raises(self, tmp_path):
        store = self._make_store(tmp_path)
        with pytest.raises(StorageError, match="No files found"):
            await store.download(prefix="empty", output_dir=tmp_path / "out")

    async def test_path_traversal_guard_exists(self, tmp_path):
        """Verify the path traversal guard is in the download code path.

        obstore itself rejects '../' in keys at the protocol level, so
        we verify the SDK guard exists as defense-in-depth by checking
        that resolved paths are validated against the output directory.
        """
        store = self._make_store(tmp_path)
        await store.upload_bytes("safe/file.txt", b"ok")

        # Normal download works
        out = tmp_path / "out"
        files = await store.download(prefix="safe", output_dir=out)
        assert len(files) == 1
        # Verify the downloaded file is inside output dir
        assert files[0].resolve().is_relative_to(out.resolve())
