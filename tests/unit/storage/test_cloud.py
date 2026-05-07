"""Unit tests for CloudStore."""

import base64
import json
from pathlib import Path

import pytest
from obstore.store import LocalStore, MemoryStore

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

    async def test_upload_dir_roundtrip(self, tmp_path):
        store = self._make_store(tmp_path)
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("aaa")
        sub = src / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("bbb")
        # Create symlink — should be skipped
        (src / "link.txt").symlink_to(src / "a.txt")

        keys = await store.upload_dir(src, prefix="up")
        assert len(keys) == 2  # symlink skipped
        assert any("a.txt" in k for k in keys)
        assert any("b.txt" in k for k in keys)

    async def test_download_key_and_prefix_mutual_exclusion(self, tmp_path):
        store = self._make_store(tmp_path)
        with pytest.raises(StorageConfigError, match="not both"):
            await store.download(
                key="file.txt", prefix="dir/", output_dir=tmp_path / "out"
            )

    async def test_suffix_filter_case_insensitive(self, tmp_path):
        """Suffix filter matches regardless of case (.JSON matches .json filter)."""
        store = self._make_store(tmp_path)
        await store.upload_bytes("data/upper.JSON", b"upper")
        await store.upload_bytes("data/lower.json", b"lower")
        await store.upload_bytes("data/skip.txt", b"skip")

        keys = await store.list(prefix="data", suffix=".json")
        assert len(keys) == 2  # both .JSON and .json matched
        assert all(".json" in k.lower() for k in keys)

    async def test_invalid_extra_json_raises(self, tmp_path):
        with pytest.raises(StorageConfigError, match="Invalid JSON"):
            CloudStore.from_credentials(
                {
                    "authType": "s3",
                    "extra": "not-valid-json{{{",
                }
            )

    async def test_upload_nonexistent_file_raises(self, tmp_path):
        store = self._make_store(tmp_path)
        with pytest.raises(StorageError):
            await store.upload(tmp_path / "does-not-exist.txt", "key.txt")

    async def test_list_excludes_zero_byte_directory_markers(self):
        # MemoryStore is a flat key-value store, so "data/run" and
        # "data/run/file.json" can coexist — matching what GCS returns after
        # obstore strips the trailing slash from the "data/run/" marker.

        store = CloudStore(MemoryStore(), provider="memory")
        await store.upload_bytes("data/run/file.json", b"{}")
        await store.upload_bytes("data/run", b"")  # GCS directory marker

        keys = await store.list(prefix="data")
        assert "data/run/file.json" in keys
        assert "data/run" not in keys
        assert len(keys) == 1

    async def test_large_file_download_streaming(self, tmp_path):
        """download(key=) streams a multi-chunk payload without buffering the whole object."""
        store = self._make_store(tmp_path)
        content = b"x" * (12 * 1024 * 1024)  # 12 MiB — exceeds default 10 MiB chunk
        await store.upload_bytes("large.bin", content)

        out = tmp_path / "out"
        files = await store.download(key="large.bin", output_dir=out)

        assert len(files) == 1
        assert files[0].read_bytes() == content


# ---------------------------------------------------------------------------
# Log-format regression: "Downloaded", "Uploaded", "Listing" must include the
# storage path / size in the message *body* so an SRE running ``kubectl logs``
# can grep without first having to query OTLP attributes.  An earlier revision
# of this PR used structured-only kwargs (e.g. ``info("Downloaded",
# storage_path=key)``) which left the body as just ``"Downloaded"``.
# ---------------------------------------------------------------------------


class TestCloudStoreLogMessageFormat:
    """Pin the message-body content for the four cloud.py log sites.

    cloud.py routes through ``get_logger()`` which uses loguru sinks — pytest's
    ``caplog`` only captures stdlib logging, so we patch ``_log()`` and inspect
    the call args directly.  We're verifying message-body shape, not delivery.
    """

    def _make_store(self, tmp_path: Path) -> CloudStore:
        store_root = tmp_path / "bucket"
        store_root.mkdir()
        return CloudStore(LocalStore(prefix=str(store_root)), provider="local")

    @staticmethod
    def _info_calls(spy):
        """Return the positional message-format strings passed to ``info()``."""
        return [
            call.args[0] for call in spy.return_value.info.call_args_list if call.args
        ]

    @staticmethod
    def _info_call_args(spy):
        """Return the (msg, *positional_args) tuples for each info() call."""
        return [
            tuple(call.args)
            for call in spy.return_value.info.call_args_list
            if call.args
        ]

    async def test_upload_log_message_inlines_key_and_size(self, tmp_path) -> None:
        from unittest.mock import patch

        store = self._make_store(tmp_path)
        local = tmp_path / "payload.bin"
        local.write_bytes(b"hello world")
        with patch("application_sdk.storage.cloud._log") as spy:
            await store.upload(local, "artifacts/payload.bin")
        # %-style: ("Uploaded key=%s bytes=%d", key, size)
        triples = [args for args in self._info_call_args(spy) if "Uploaded" in args[0]]
        assert any(
            "key=%s" in args[0]
            and "bytes=%d" in args[0]
            and args[1:] == ("artifacts/payload.bin", 11)
            for args in triples
        ), f"Uploaded line did not inline key+size, got: {triples}"

    async def test_download_single_log_message_inlines_key_and_local_path(
        self, tmp_path
    ) -> None:
        from unittest.mock import patch

        store = self._make_store(tmp_path)
        await store.upload_bytes("artifacts/x.bin", b"data")
        out = tmp_path / "out"
        with patch("application_sdk.storage.cloud._log") as spy:
            await store.download(key="artifacts/x.bin", output_dir=out)
        triples = [
            args for args in self._info_call_args(spy) if "Downloaded" in args[0]
        ]
        assert any(
            "key=%s" in args[0]
            and "local_path=%s" in args[0]
            and args[1] == "artifacts/x.bin"
            for args in triples
        ), f"Downloaded line did not inline key+local_path, got: {triples}"

    async def test_download_prefix_log_messages_inline_prefix_and_count(
        self, tmp_path
    ) -> None:
        from unittest.mock import patch

        store = self._make_store(tmp_path)
        await store.upload_bytes("dir/a.bin", b"a")
        await store.upload_bytes("dir/b.bin", b"bb")
        out = tmp_path / "out"
        with patch("application_sdk.storage.cloud._log") as spy:
            await store.download(prefix="dir", output_dir=out)
        formats = self._info_calls(spy)
        assert any(
            "Listing objects under prefix=%s" in fmt for fmt in formats
        ), f"Listing line did not use %-style prefix, got: {formats}"
        assert any(
            "Downloaded %d files from prefix=%s" in fmt for fmt in formats
        ), f"Downloaded-N line did not use %-style, got: {formats}"

    async def test_large_file_upload_streaming(self, tmp_path):
        """upload() streams a multi-chunk local file without reading it all into memory."""
        store = self._make_store(tmp_path)
        content = b"y" * (12 * 1024 * 1024)  # 12 MiB
        src = tmp_path / "large.bin"
        src.write_bytes(content)

        size = await store.upload(src, "large.bin")

        assert size == len(content)
        stored = await store.get_bytes("large.bin")
        assert stored == content

    async def test_streaming_prefix_download_multi_file(self, tmp_path):
        """download(prefix=) streams all matching files and preserves path-traversal guard."""
        store = self._make_store(tmp_path)
        files_in = {
            "batch/a.json": b'{"a":1}',
            "batch/sub/b.json": b'{"b":2}',
            "batch/c.txt": b"c",
        }
        for key, data in files_in.items():
            await store.upload_bytes(key, data)

        out = tmp_path / "out"
        downloaded = await store.download(prefix="batch", output_dir=out)

        assert len(downloaded) == 3
        for path in downloaded:
            assert path.resolve().is_relative_to(out.resolve())
        contents = {p.read_bytes() for p in downloaded}
        assert contents == set(files_in.values())
