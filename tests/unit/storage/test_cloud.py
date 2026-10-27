"""Unit tests for CloudStore."""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from obstore.exceptions import PermissionDeniedError, UnauthenticatedError
from obstore.store import GCSStore, LocalStore, MemoryStore

from application_sdk.storage import cloud as _cloud_module
from application_sdk.storage.cloud import CloudStore, _infer_auth_type
from application_sdk.storage.errors import (
    StorageAuthError,
    StorageConfigError,
    StorageError,
    StorageGatewayAuthUnavailableError,
    StorageNotFoundError,
    StoragePermissionError,
)


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


_REAL_LOOKUP_BUCKET_REGION = _cloud_module._lookup_bucket_region


@pytest.fixture(autouse=True)
def _no_region_lookup(monkeypatch):
    monkeypatch.setattr(_cloud_module, "_lookup_bucket_region", _async_return(""))


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

    @patch(
        "application_sdk.storage._credential_providers._UtcExpiryStsCredentialProvider"
    )
    @patch("boto3.Session")
    @patch("obstore.store.S3Store")
    def test_s3_role_arn_wires_assume_role_provider(
        self,
        mock_s3_cls: MagicMock,
        mock_session_cls: MagicMock,
        mock_sts_cls: MagicMock,
    ):
        """BLDX-1441: cross-account ``aws_role_arn`` with no base keys must wire
        an STS assume-role credential provider against the *user-entered* bucket.

        obstore has no static ``aws_role_arn`` config key, so stuffing it into
        ``config`` is a silent no-op — the store falls back to the pod's ambient
        (tenant) identity and can't reach the customer's cross-account bucket.
        The role must be assumed via a credential provider (as ``binding.py`` does).
        """
        mock_s3_cls.return_value = MagicMock()
        mock_sts_cls.return_value = MagicMock()
        mock_session_cls.return_value = MagicMock()

        store = CloudStore.from_credentials(
            {
                "authType": "s3",
                "extra": {
                    "s3_bucket": "customer-cross-account-bucket",
                    "aws_role_arn": "arn:aws:iam::222222222222:role/CrossAccount",
                    "region": "us-east-1",
                },
            }
        )

        assert store.provider == "s3"
        # The STS assume-role provider was actually constructed (not skipped).
        mock_sts_cls.assert_called_once()
        call_kwargs = mock_s3_cls.call_args.kwargs
        # The user-entered bucket is honored, not swapped for a default.
        assert call_kwargs["bucket"] == "customer-cross-account-bucket"
        # An assume-role credential provider is wired onto the store.
        assert call_kwargs.get("credential_provider") is not None
        # And the role is NOT smuggled in as an inert obstore config key.
        config = call_kwargs.get("config") or {}
        assert "aws_role_arn" not in config
        assert "aws_role_session_name" not in config
        # Region stays on the S3 store's config; must not reach the STS session.
        assert config.get("aws_region") == "us-east-1"
        session_kwargs = mock_session_cls.call_args.kwargs
        assert "region_name" not in session_kwargs

    @pytest.mark.parametrize(
        ("external_id", "expected"),
        [
            ("ext-id-123", "ext-id-123"),
            ("  ext-id-123  ", "ext-id-123"),
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    @patch(
        "application_sdk.storage._credential_providers._UtcExpiryStsCredentialProvider"
    )
    @patch("boto3.Session")
    @patch("obstore.store.S3Store")
    def test_s3_role_arn_forwards_external_id(
        self,
        mock_s3_cls: MagicMock,
        mock_session_cls: MagicMock,
        mock_sts_cls: MagicMock,
        external_id: str | None,
        expected: str | None,
    ):
        """``extra.aws_external_id`` reaches the STS AssumeRole call as
        ``ExternalId``; blank values are dropped (STS rejects an empty one)."""
        extra = {
            "s3_bucket": "customer-cross-account-bucket",
            "aws_role_arn": "arn:aws:iam::222222222222:role/CrossAccount",
        }
        if external_id is not None:
            extra["aws_external_id"] = external_id

        CloudStore.from_credentials({"authType": "s3", "extra": extra})

        sts_kwargs = mock_sts_cls.call_args.kwargs
        assert sts_kwargs["RoleArn"] == "arn:aws:iam::222222222222:role/CrossAccount"
        if expected is None:
            assert "ExternalId" not in sts_kwargs
        else:
            assert sts_kwargs["ExternalId"] == expected
        config = mock_s3_cls.call_args.kwargs.get("config") or {}
        assert "aws_external_id" not in config

    @patch(
        "application_sdk.storage._credential_providers._UtcExpiryStsCredentialProvider"
    )
    @patch("obstore.store.S3Store")
    def test_s3_external_id_ignored_without_role_arn(
        self, mock_s3_cls: MagicMock, mock_sts_cls: MagicMock
    ):
        """Without a role ARN, static keys are used and no STS call is wired."""
        CloudStore.from_credentials(
            {
                "authType": "s3",
                "username": "AKID",
                "password": "secret",
                "extra": {"s3_bucket": "test-bucket", "aws_external_id": "ext-id-123"},
            }
        )

        mock_sts_cls.assert_not_called()
        call_kwargs = mock_s3_cls.call_args.kwargs
        assert call_kwargs.get("credential_provider") is None
        assert call_kwargs["config"]["aws_access_key_id"] == "AKID"
        assert "aws_external_id" not in call_kwargs["config"]

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

    async def test_upload_writes_no_sidecar(self, tmp_path):
        """Routing through ``upload_file`` must not drag the sidecar protocol
        onto an external bucket: the customer's store gains only the object
        they asked for."""
        store = self._make_store(tmp_path)
        local = tmp_path / "payload.bin"
        local.write_bytes(b"0123456789")
        await store.upload(local, "artifacts/payload.bin")
        assert await store.list(prefix="artifacts") == ["artifacts/payload.bin"]

    async def test_upload_detects_local_truncation_mid_read(self, tmp_path):
        """A file truncated under the reader leaves a prefix of the intended
        artifact in the customer's bucket — that must not report success
        (FND-306). This is the guarantee routing through the shared primitive
        buys; it fails if ``upload`` ever streams directly again."""
        from application_sdk.storage.errors import StorageIntegrityError

        store = self._make_store(tmp_path)
        local = tmp_path / "big.bin"
        local.write_bytes(b"x" * 4096)

        real_open = Path.open

        def truncating_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            with open(self, "r+b") as trunc:
                trunc.truncate(16)
            return handle

        with (
            patch.object(Path, "open", truncating_open),
            pytest.raises(StorageIntegrityError) as exc,
        ):
            await store.upload(local, "artifacts/big.bin")
        assert exc.value.check == "local_size"
        assert exc.value.effective_retryable is False

    async def test_upload_detects_object_dropped_by_the_store(self, tmp_path):
        """A backend that reports success but persists nothing must not pass
        silently on an external bucket either."""
        store = self._make_store(tmp_path)
        local = tmp_path / "payload.bin"
        local.write_bytes(b"0123456789")

        with (
            patch(
                "application_sdk.storage.ops.get_file_meta",
                new=_async_return(None),
            ),
            pytest.raises(StorageError, match="absent from the store"),
        ):
            await store.upload(local, "artifacts/payload.bin")

    async def test_upload_detects_short_readback(self, tmp_path):
        store = self._make_store(tmp_path)
        local = tmp_path / "payload.bin"
        local.write_bytes(b"0123456789")

        with (
            patch(
                "application_sdk.storage.ops.get_file_meta",
                new=_async_return((4, "etag")),
            ),
            pytest.raises(StorageError, match="Incomplete upload"),
        ):
            await store.upload(local, "artifacts/payload.bin")

    async def test_list_empty_prefix(self, tmp_path):
        store = self._make_store(tmp_path)
        keys = await store.list(prefix="nothing")
        assert keys == []

    async def test_download_empty_prefix_raises(self, tmp_path):
        store = self._make_store(tmp_path)
        with pytest.raises(StorageError, match="No files found"):
            await store.download(prefix="empty", output_dir=tmp_path / "out")

    async def test_download_prefix_skips_childless_folder_marker(self, tmp_path):
        import obstore

        store = CloudStore(MemoryStore(), provider="memory")
        await store.upload_bytes("dump/base/result.json", b"{}")
        await store.upload_bytes("dump/zero.json", b"")
        real_list = obstore.list

        async def _list(raw, prefix=None, **kwargs):
            async for batch in real_list(raw, prefix=prefix, **kwargs):
                yield batch
            yield [{"path": "dump/empty", "size": 0, "e_tag": None}]

        out = tmp_path / "out"
        with patch.object(obstore, "list", _list):
            assert await store.list("dump") == [
                "dump/base/result.json",
                "dump/zero.json",
            ]
            paths = await store.download(prefix="dump", output_dir=out)
        assert sorted(p.relative_to(out).as_posix() for p in paths) == [
            "base/result.json",
            "zero.json",
        ]

    async def test_download_empty_prefix_preserves_subdirectories(self, tmp_path):
        """A whole-bucket download (prefix="") must not flatten same-named files
        from different subdirectories onto one path.

        Regression test: list_prefix is "" for prefix="", and obj_key.startswith("")
        is always True, so each key's full path — not just its basename — must be
        kept. Before the fix, an `and list_prefix` guard forced every object here
        onto the basename-only branch, so both manifest.json files below clobbered
        onto a single downloaded file.
        """
        store = self._make_store(tmp_path)
        await store.upload_bytes("client-a/manifest.json", b"client-a-manifest")
        await store.upload_bytes("client-b/manifest.json", b"client-b-manifest")

        out = tmp_path / "out"
        downloaded = await store.download(prefix="", output_dir=out)

        assert len(downloaded) == 2
        contents = {p.read_bytes() for p in downloaded}
        assert contents == {b"client-a-manifest", b"client-b-manifest"}

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


class TestCloudStorePutAttributes:
    """storageClass in extra is forwarded as a put attribute on every write."""

    @pytest.mark.asyncio
    async def test_from_credentials_s3_storage_class_passed_to_upload(
        self, tmp_path
    ) -> None:
        """from_credentials with storageClass → upload() calls open_writer_async with attributes."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.storage.cloud import CloudStore

        mock_writer = AsyncMock()
        mock_writer.__aenter__ = AsyncMock(return_value=mock_writer)
        mock_writer.__aexit__ = AsyncMock(return_value=False)

        src = tmp_path / "f.bin"
        src.write_bytes(b"hello")

        with (
            patch("application_sdk.storage.cloud._create_s3_store") as mock_create,
            patch(
                "obstore.open_writer_async", return_value=mock_writer
            ) as mock_writer_call,
            # The post-upload integrity readback would call head_async on the
            # mocked store; stub it out — attribute forwarding is what's under
            # test here, not the FND-306 readback (covered in test_integrity).
            patch(
                "application_sdk.storage.ops.get_file_meta",
                new=_async_return((5, "etag")),
            ),
        ):
            mock_store = MagicMock()
            put_attrs = {"Storage-Class": "STANDARD_IA"}
            mock_create.return_value = (mock_store, put_attrs)

            cs = CloudStore.from_credentials(
                {
                    "authType": "s3",
                    "username": "KEY",
                    "password": "SECRET",
                    "extra": {"s3_bucket": "b", "storageClass": "STANDARD_IA"},
                }
            )
            await cs.upload(src, "out/f.bin")

        _call_kwargs = mock_writer_call.call_args
        assert _call_kwargs.kwargs.get("attributes") == put_attrs or (
            len(_call_kwargs.args) > 2 and _call_kwargs.args[2] == put_attrs
        ), f"attributes not forwarded; call was {_call_kwargs}"

    @pytest.mark.asyncio
    async def test_from_credentials_s3_storage_class_passed_to_upload_bytes(
        self,
    ) -> None:
        """from_credentials with storageClass → upload_bytes() calls put_async with attributes."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.storage.cloud import CloudStore

        with (
            patch("application_sdk.storage.cloud._create_s3_store") as mock_create,
            patch("obstore.put_async", new_callable=AsyncMock) as mock_put,
        ):
            mock_store = MagicMock()
            put_attrs = {"Storage-Class": "STANDARD_IA"}
            mock_create.return_value = (mock_store, put_attrs)

            cs = CloudStore.from_credentials(
                {
                    "authType": "s3",
                    "username": "KEY",
                    "password": "SECRET",
                    "extra": {"s3_bucket": "b", "storageClass": "STANDARD_IA"},
                }
            )
            await cs.upload_bytes("out/f.bin", b"hello")

        mock_put.assert_awaited_once()
        _, call_kwargs = mock_put.call_args[0], mock_put.call_args
        assert call_kwargs.kwargs.get("attributes") == put_attrs

    def test_from_credentials_s3_no_storage_class_gives_none_put_attrs(self) -> None:
        """When storageClass is absent, _put_attributes is None."""
        from unittest.mock import MagicMock, patch

        from application_sdk.storage.cloud import CloudStore

        with patch("application_sdk.storage.cloud._create_s3_store") as mock_create:
            mock_create.return_value = (MagicMock(), None)
            cs = CloudStore.from_credentials(
                {
                    "authType": "s3",
                    "username": "K",
                    "password": "S",
                    "extra": {"s3_bucket": "b"},
                }
            )

        assert cs._put_attributes is None

    def test_from_credentials_gcs_storage_class_put_attrs(self) -> None:
        """storageClass in extra is forwarded as X-Goog-Storage-Class put attribute for GCS."""
        from unittest.mock import MagicMock, patch

        from application_sdk.storage.cloud import CloudStore

        with patch("application_sdk.storage.cloud._create_gcs_store") as mock_create:
            put_attrs = {"X-Goog-Storage-Class": "NEARLINE"}
            mock_create.return_value = (MagicMock(), put_attrs)
            cs = CloudStore.from_credentials(
                {
                    "authType": "gcs",
                    "extra": {"gcs_bucket": "b", "storageClass": "NEARLINE"},
                }
            )

        assert cs._put_attributes == {"X-Goog-Storage-Class": "NEARLINE"}

    def test_from_credentials_gcs_no_storage_class_gives_none_put_attrs(self) -> None:
        """When storageClass is absent on GCS, _put_attributes is None."""
        from unittest.mock import MagicMock, patch

        from application_sdk.storage.cloud import CloudStore

        with patch("application_sdk.storage.cloud._create_gcs_store") as mock_create:
            mock_create.return_value = (MagicMock(), None)
            cs = CloudStore.from_credentials(
                {"authType": "gcs", "extra": {"gcs_bucket": "b"}}
            )

        assert cs._put_attributes is None

    def test_from_credentials_azure_storage_class_put_attrs(self) -> None:
        """storageClass in extra is forwarded as x-ms-access-tier put attribute for Azure."""
        from unittest.mock import MagicMock, patch

        from application_sdk.storage.cloud import CloudStore

        with patch("application_sdk.storage.cloud._create_azure_store") as mock_create:
            put_attrs = {"x-ms-access-tier": "Cool"}
            mock_create.return_value = (MagicMock(), put_attrs)
            cs = CloudStore.from_credentials(
                {
                    "authType": "adls",
                    "password": "key==",
                    "extra": {
                        "storage_account_name": "acct",
                        "storageClass": "Cool",
                    },
                }
            )

        assert cs._put_attributes == {"x-ms-access-tier": "Cool"}

    def test_from_credentials_azure_no_storage_class_gives_none_put_attrs(self) -> None:
        """When storageClass is absent on Azure, _put_attributes is None."""
        from unittest.mock import MagicMock, patch

        from application_sdk.storage.cloud import CloudStore

        with patch("application_sdk.storage.cloud._create_azure_store") as mock_create:
            mock_create.return_value = (MagicMock(), None)
            cs = CloudStore.from_credentials(
                {
                    "authType": "adls",
                    "password": "key==",
                    "extra": {"storage_account_name": "acct"},
                }
            )

        assert cs._put_attributes is None


class TestS3RegionLookup:
    @staticmethod
    def _stub_head(monkeypatch, *, headers=None, error=None, by_host=None):
        import aiohttp

        seen: dict = {"urls": []}

        class _Response:
            def __init__(self, response_headers):
                self.headers = response_headers

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        def _head(_self, url, **kwargs):
            seen["urls"].append(url)
            seen.update(url=url, **kwargs)
            if error is not None:
                raise error
            if by_host is not None:
                return _Response(by_host.get(url.split("/")[2], {}))
            return _Response(headers or {})

        monkeypatch.setattr(aiohttp.ClientSession, "head", _head)
        return seen

    @pytest.mark.parametrize(
        ("headers", "expected", "urls"),
        [
            (
                {"x-amz-bucket-region": "us-west-2"},
                "us-west-2",
                ["https://s3.amazonaws.com/my.bucket"],
            ),
            (
                {},
                "",
                [
                    "https://s3.amazonaws.com/my.bucket",
                    "https://s3.cn-north-1.amazonaws.com.cn/my.bucket",
                ],
            ),
        ],
    )
    async def test_lookup_reads_region_header(
        self, monkeypatch, headers, expected, urls
    ):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        seen = self._stub_head(monkeypatch, headers=headers)
        assert await _REAL_LOOKUP_BUCKET_REGION("my.bucket") == expected
        assert seen["urls"] == urls
        assert seen["allow_redirects"] is False

    @pytest.mark.parametrize(
        ("env", "region_host", "expected"),
        [
            ({}, "s3.cn-north-1.amazonaws.com.cn", "cn-northwest-1"),
            (
                {"AWS_REGION": "us-east-1"},
                "s3.cn-north-1.amazonaws.com.cn",
                "cn-north-1",
            ),
            ({"AWS_REGION": "cn-north-1"}, "s3.amazonaws.com", "eu-west-1"),
        ],
    )
    async def test_lookup_falls_back_to_other_partition(
        self, monkeypatch, env, region_host, expected
    ):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        seen = self._stub_head(
            monkeypatch,
            by_host={region_host: {"x-amz-bucket-region": expected}},
        )
        assert await _REAL_LOOKUP_BUCKET_REGION("b") == expected
        assert len(seen["urls"]) == 2
        assert seen["urls"][-1] == f"https://{region_host}/b"

    @pytest.mark.parametrize(
        "error", [OSError("unreachable"), RuntimeError("socket blocked")]
    )
    async def test_lookup_failure_returns_empty(self, monkeypatch, error):
        import aiohttp

        seen = self._stub_head(
            monkeypatch,
            error=aiohttp.ClientConnectionError("unreachable")
            if isinstance(error, OSError)
            else error,
        )
        assert await _REAL_LOOKUP_BUCKET_REGION("b") == ""
        assert len(seen["urls"]) == 1

    @staticmethod
    def _regions_built(monkeypatch):
        from application_sdk.storage import _obstore_config

        built: list[str | None] = []

        def _make(bucket, config=None, **_kwargs):
            built.append((config or {}).get("aws_region"))
            return MemoryStore()

        monkeypatch.setattr(_obstore_config, "make_s3_store", _make)
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT", raising=False)
        return built

    async def test_blank_region_rebuilds_store_once_with_bucket_region(
        self, monkeypatch
    ):
        import asyncio

        from application_sdk.storage import cloud

        built = self._regions_built(monkeypatch)

        async def _slow_lookup(_bucket):
            await asyncio.sleep(0)
            return "us-west-2"

        lookup = MagicMock(side_effect=_slow_lookup)
        monkeypatch.setattr(cloud, "_lookup_bucket_region", lookup)
        store = CloudStore.from_credentials(
            {"authType": "s3", "extra": {"s3_bucket": "b"}}
        )
        await asyncio.gather(store.list("x"), store.list("y"))
        assert built == [None, "us-west-2"]
        lookup.assert_called_once_with("b")

    async def test_explicit_region_skips_lookup(self, monkeypatch):
        from application_sdk.storage import cloud

        built = self._regions_built(monkeypatch)
        lookup = MagicMock(side_effect=_async_return("us-west-2"))
        monkeypatch.setattr(cloud, "_lookup_bucket_region", lookup)
        store = CloudStore.from_credentials(
            {"authType": "s3", "extra": {"s3_bucket": "b", "region": "eu-west-1"}}
        )
        await store.list("x")
        assert built == ["eu-west-1"]
        lookup.assert_not_called()

    async def test_custom_endpoint_skips_lookup(self, monkeypatch):
        from application_sdk.storage import cloud

        built = self._regions_built(monkeypatch)
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9000")
        lookup = MagicMock(side_effect=_async_return("us-west-2"))
        monkeypatch.setattr(cloud, "_lookup_bucket_region", lookup)
        store = CloudStore.from_credentials(
            {"authType": "s3", "extra": {"s3_bucket": "b"}}
        )
        await store.list("x")
        assert built == [None]
        lookup.assert_not_called()

    async def test_failed_lookup_keeps_store(self, monkeypatch):
        from application_sdk.storage import cloud

        built = self._regions_built(monkeypatch)
        monkeypatch.setattr(cloud, "_lookup_bucket_region", _async_return(""))
        store = CloudStore.from_credentials(
            {"authType": "s3", "extra": {"s3_bucket": "b"}}
        )
        before = store.store
        await store.list("x")
        assert built == [None]
        assert store.store is before

    @pytest.mark.parametrize(
        "call",
        [
            lambda store, tmp: store.get_bytes("k"),
            lambda store, tmp: store.download(prefix="p", output_dir=tmp / "out"),
            lambda store, tmp: store.list("p"),
            lambda store, tmp: store.upload(tmp / "f.bin", "k"),
            lambda store, tmp: store.upload_bytes("k", b"x"),
            lambda store, tmp: store.upload_dir(tmp / "d", "p"),
        ],
        ids=["get_bytes", "download", "list", "upload", "upload_bytes", "upload_dir"],
    )
    async def test_every_entry_point_resolves_region_first(
        self, monkeypatch, tmp_path, call
    ):
        from application_sdk.storage import cloud

        built = self._regions_built(monkeypatch)
        monkeypatch.setattr(cloud, "_lookup_bucket_region", _async_return("us-west-2"))
        (tmp_path / "f.bin").write_bytes(b"x")
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "g.bin").write_bytes(b"y")
        store = CloudStore.from_credentials(
            {"authType": "s3", "extra": {"s3_bucket": "b"}}
        )
        try:
            await call(store, tmp_path)
        except StorageError:
            pass
        assert built == [None, "us-west-2"]

    @pytest.mark.parametrize(
        ("env", "hosts"),
        [
            (
                {"AWS_REGION": "eu-west-1"},
                ["s3.eu-west-1.amazonaws.com", "s3.cn-north-1.amazonaws.com.cn"],
            ),
            (
                {"AWS_DEFAULT_REGION": "ap-south-1"},
                ["s3.ap-south-1.amazonaws.com", "s3.cn-north-1.amazonaws.com.cn"],
            ),
            (
                {"AWS_REGION": "cn-northwest-1"},
                ["s3.cn-northwest-1.amazonaws.com.cn", "s3.amazonaws.com"],
            ),
            ({}, ["s3.amazonaws.com", "s3.cn-north-1.amazonaws.com.cn"]),
        ],
    )
    def test_lookup_uses_ambient_regional_endpoint(self, monkeypatch, env, hosts):
        from application_sdk.storage import cloud

        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert cloud._region_lookup_hosts() == hosts

    async def test_lookup_uses_sdk_ssl_context(self, monkeypatch):
        from application_sdk.clients import ssl_utils

        sentinel = object()
        monkeypatch.setattr(ssl_utils, "get_ssl_context", lambda: sentinel)
        seen = self._stub_head(monkeypatch, headers={})
        await _REAL_LOOKUP_BUCKET_REGION("b")
        assert seen["ssl"] is sentinel

    async def test_rebuild_uses_credentials_as_given(self, monkeypatch):
        from application_sdk.storage import _obstore_config, cloud

        built: list[tuple[str, dict]] = []

        def _make(bucket, config=None, **_kwargs):
            built.append((bucket, dict(config or {})))
            return MemoryStore()

        monkeypatch.setattr(_obstore_config, "make_s3_store", _make)
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT", raising=False)
        monkeypatch.setattr(cloud, "_lookup_bucket_region", _async_return("us-west-2"))
        credentials = {
            "authType": "s3",
            "username": "access-a",
            "password": "secret-a",
            "extra": {"s3_bucket": "b"},
        }
        store = CloudStore.from_credentials(credentials)
        credentials["username"] = "access-b"
        credentials["extra"]["s3_bucket"] = "other"
        await store.list("x")
        bucket, config = built[-1]
        assert bucket == "b"
        assert config["aws_access_key_id"] == "access-a"
        assert config["aws_region"] == "us-west-2"

    async def test_download_checks_arguments_before_lookup(self, monkeypatch):
        from application_sdk.storage import cloud

        self._regions_built(monkeypatch)
        lookup = MagicMock(side_effect=_async_return("us-west-2"))
        monkeypatch.setattr(cloud, "_lookup_bucket_region", lookup)
        store = CloudStore.from_credentials(
            {"authType": "s3", "extra": {"s3_bucket": "b"}}
        )
        with pytest.raises(StorageConfigError):
            await store.download(key="k", prefix="p")
        lookup.assert_not_called()


_GCS_VPC_SC_403 = (
    "Generic GCS error: Error performing list request: Error performing GET "
    "https://storage.googleapis.com/storage/v1/b/example-bucket/o: Client error "
    "with status 403 Forbidden: <?xml version='1.0'?><Error>"
    "<Code>SecurityPolicyViolated</Code>"
    "<Message>Request violates VPC Service Controls.</Message></Error>"
)
_S3_ACCESS_DENIED_403 = (
    "Generic S3 error: Error performing list request: non-2xx status code: 403 "
    "<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
)


class TestListPermissionDenied:
    """A 401/403 listing a bucket is the caller's to fix, not a retryable outage."""

    @pytest.fixture(params=["_list_keys", "_list_keys_with_meta"])
    def list_fn(self, request):
        return request.param

    async def _list(self, monkeypatch, list_fn, message, provider="gcs"):
        async def _boom(*_a, **_k):
            raise RuntimeError(message)

        monkeypatch.setattr(_cloud_module, "_list_items", _boom)
        store = CloudStore(
            GCSStore("example-bucket", skip_signature=True), provider=provider
        )
        return await getattr(store, list_fn)("prefix/")

    async def test_gcs_vpc_sc_403_is_user_permission_error(self, monkeypatch, list_fn):
        with pytest.raises(StoragePermissionError) as ei:
            await self._list(monkeypatch, list_fn, _GCS_VPC_SC_403)
        err = ei.value
        assert err.effective_retryable is False
        assert err.audience.name == "USER"
        assert err.http_status == 403
        assert err.provider_code == "SecurityPolicyViolated"
        assert err.target == "gs://example-bucket/prefix/"
        assert "VPC Service Controls" in str(err)

    async def test_s3_access_denied_403_is_permission_error(self, monkeypatch, list_fn):
        with pytest.raises(StoragePermissionError) as ei:
            await self._list(monkeypatch, list_fn, _S3_ACCESS_DENIED_403, provider="s3")
        assert ei.value.provider_code == "AccessDenied"
        assert ei.value.http_status == 403

    async def test_503_stays_retryable_storage_error(self, monkeypatch, list_fn):
        with pytest.raises(StorageError) as ei:
            await self._list(
                monkeypatch, list_fn, "non-2xx status code: 503 Service Unavailable"
            )
        assert not isinstance(ei.value, StoragePermissionError)
        assert ei.value.effective_retryable is True

    async def test_typed_obstore_denial_without_status_text(self, monkeypatch, list_fn):
        async def _boom(*_a, **_k):
            raise UnauthenticatedError("credential rejected")

        monkeypatch.setattr(_cloud_module, "_list_items", _boom)
        store = CloudStore(
            GCSStore("example-bucket", skip_signature=True), provider="gcs"
        )
        with pytest.raises(StorageAuthError) as ei:
            await getattr(store, list_fn)("prefix/")
        assert ei.value.http_status == 401
        assert ei.value.provider_code is None


def _gcs_store() -> CloudStore:
    return CloudStore(GCSStore("example-bucket", skip_signature=True), provider="gcs")


def _wrapped_403(key: str) -> StorageError:
    """A 403 as the shared primitives deliver it: a retryable StorageError chained to the driver error."""
    try:
        raise RuntimeError(_GCS_VPC_SC_403)
    except RuntimeError as driver:
        err = StorageError(f"Failed on {key}", key=key, cause=driver)
        err.__cause__ = driver
        return err


class TestReadWritePermissionDenied:
    """Reads and writes map a 401/403 the same way listing does."""

    async def test_get_bytes_403(self, monkeypatch):
        async def _boom(*_a, **_k):
            raise PermissionDeniedError(_GCS_VPC_SC_403)

        monkeypatch.setattr(_cloud_module.obs, "get_async", _boom)
        with pytest.raises(StoragePermissionError) as ei:
            await _gcs_store().get_bytes("data/a.json")
        assert ei.value.target == "gs://example-bucket/data/a.json"
        assert ei.value.provider_code == "SecurityPolicyViolated"
        assert ei.value.effective_retryable is False

    async def test_upload_bytes_403(self, monkeypatch):
        async def _boom(*_a, **_k):
            raise RuntimeError(_S3_ACCESS_DENIED_403)

        monkeypatch.setattr(_cloud_module.obs, "put_async", _boom)
        with pytest.raises(StoragePermissionError) as ei:
            await _gcs_store().upload_bytes("out/a.json", b"x")
        assert ei.value.provider_code == "AccessDenied"

    async def test_download_single_wrapped_403(self, monkeypatch, tmp_path):
        async def _boom(key, *_a, **_k):
            raise _wrapped_403(key)

        monkeypatch.setattr(_cloud_module, "download_file_chunked", _boom)
        with pytest.raises(StoragePermissionError) as ei:
            await _gcs_store().download(key="data/a.json", output_dir=tmp_path)
        assert ei.value.http_status == 403
        assert ei.value.key == "data/a.json"

    async def test_upload_wrapped_403(self, monkeypatch, tmp_path):
        src = tmp_path / "a.json"
        src.write_bytes(b"x")

        async def _boom(key, *_a, **_k):
            raise _wrapped_403(key)

        monkeypatch.setattr(_cloud_module, "upload_file", _boom)
        with pytest.raises(StoragePermissionError):
            await _gcs_store().upload(src, "out/a.json")

    async def test_wrapped_503_is_reraised_unchanged(self, monkeypatch, tmp_path):
        original = StorageError("Failed", key="data/a.json")

        async def _boom(*_a, **_k):
            raise original

        monkeypatch.setattr(_cloud_module, "download_file_chunked", _boom)
        with pytest.raises(StorageError) as ei:
            await _gcs_store().download(key="data/a.json", output_dir=tmp_path)
        assert ei.value is original


# An Azure service-principal secret the identity provider refused. obstore
# reports the failed token request as a GenericError, so only the status text
# says it was a 401.
_AZURE_TOKEN_401 = (
    "Generic MicrosoftAzure error: Error performing token request: Error "
    "performing POST https://login.microsoftonline.com/example-tenant/oauth2/v2.0/"
    "token in 77ms - Server returned non-2xx status code: 401 Unauthorized: "
    '{"error":"invalid_client","error_description":"AADSTS7000215: Invalid '
    'client secret provided.","error_codes":[7000215]}'
)
_GATEWAY_1005_401 = (
    "Generic S3 error: Error performing GET request: non-2xx status code: 401 "
    '{"code": 1005, "error": "Invalid Client"}'
)


def _wrapped(message: str, key: str) -> StorageError:
    """A driver error as the head path delivers it: a StorageError chained to it."""
    try:
        raise RuntimeError(message)
    except RuntimeError as driver:
        err = StorageError(f"Failed to head key '{key}'", key=key, cause=driver)
        err.__cause__ = driver
        return err


class TestAuthenticationRejected:
    """A 401 is the credential itself being rejected: AUTH, not PERMISSION."""

    async def test_download_single_azure_token_401(self, monkeypatch, tmp_path):
        async def _boom(key, *_a, **_k):
            raise _wrapped(_AZURE_TOKEN_401, key)

        monkeypatch.setattr(_cloud_module, "download_file_chunked", _boom)
        with pytest.raises(StorageAuthError) as ei:
            await _gcs_store().download(key="specs/api.json", output_dir=tmp_path)
        err = ei.value
        assert err.category.name == "AUTH"
        assert err.audience.name == "USER"
        assert err.effective_retryable is False
        assert err.http_status == 401
        assert err.key == "specs/api.json"
        assert "secret value rather than the secret ID" in str(err)

    async def test_auth_error_is_still_a_permission_error(self, monkeypatch):
        """Existing ``except StoragePermissionError`` blocks keep catching a 401."""

        async def _boom(*_a, **_k):
            raise RuntimeError(_AZURE_TOKEN_401)

        monkeypatch.setattr(_cloud_module.obs, "get_async", _boom)
        with pytest.raises(StoragePermissionError):
            await _gcs_store().get_bytes("specs/api.json")

    async def test_403_stays_permission_not_auth(self, monkeypatch):
        async def _boom(*_a, **_k):
            raise PermissionDeniedError(_GCS_VPC_SC_403)

        monkeypatch.setattr(_cloud_module.obs, "get_async", _boom)
        with pytest.raises(StoragePermissionError) as ei:
            await _gcs_store().get_bytes("data/a.json")
        assert not isinstance(ei.value, StorageAuthError)
        assert ei.value.category.name == "PERMISSION"

    async def test_gateway_leaf_is_reraised_unchanged(self, monkeypatch, tmp_path):
        original = StorageGatewayAuthUnavailableError(
            "gateway lookup failed", key="data/a.json", http_status=401
        )

        async def _boom(*_a, **_k):
            raise original

        monkeypatch.setattr(_cloud_module, "download_file_chunked", _boom)
        with pytest.raises(StorageError) as ei:
            await _gcs_store().download(key="data/a.json", output_dir=tmp_path)
        assert ei.value is original

    async def test_raw_gateway_1005_is_not_a_credential_error(self, monkeypatch):
        async def _boom(*_a, **_k):
            raise RuntimeError(_GATEWAY_1005_401)

        monkeypatch.setattr(_cloud_module.obs, "get_async", _boom)
        with pytest.raises(StorageError) as ei:
            await _gcs_store().get_bytes("data/a.json")
        assert not isinstance(ei.value, StoragePermissionError)
        assert ei.value.effective_retryable is True
