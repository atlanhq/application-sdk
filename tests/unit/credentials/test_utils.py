import base64
import os
from unittest.mock import AsyncMock, patch

from application_sdk.credentials.utils import resolve_credential_file


class TestResolveCredentialFile:
    """Tests for resolve_credential_file() — handles both object-store refs and base64 content."""

    # ------------------------------------------------------------------
    # Object-store reference path (delegates to download_file_from_upload_response)
    # ------------------------------------------------------------------

    @patch(
        "application_sdk.credentials.utils.download_file_from_upload_response",
        new_callable=AsyncMock,
    )
    async def test_object_store_reference_with_key(self, mock_download, tmp_path):
        """JSON with 'key' field routes to download_file_from_upload_response."""
        mock_download.return_value = str(tmp_path / "keytab.keytab")
        value = '{"key": "artifacts/hiveadmin.keytab", "rawName": "hiveadmin.keytab", "extension": ".keytab"}'

        result = await resolve_credential_file(value, "keytab.keytab", str(tmp_path))

        mock_download.assert_awaited_once_with(value)
        assert result == str(tmp_path / "keytab.keytab")

    @patch(
        "application_sdk.credentials.utils.download_file_from_upload_response",
        new_callable=AsyncMock,
    )
    async def test_object_store_reference_with_filekey(self, mock_download, tmp_path):
        """JSON with 'fileKey' field routes to download_file_from_upload_response."""
        mock_download.return_value = str(tmp_path / "krb5.conf")
        value = '{"fileKey": "artifacts/krb5.conf", "rawName": "krb5.conf"}'

        result = await resolve_credential_file(value, "krb5.conf", str(tmp_path))

        mock_download.assert_awaited_once_with(value)
        assert result == str(tmp_path / "krb5.conf")

    # ------------------------------------------------------------------
    # Base64 content path (SDR / secret store)
    # ------------------------------------------------------------------

    async def test_base64_binary_file_written_correctly(self, tmp_path):
        """Valid base64 binary content is decoded and written to disk."""
        original_bytes = (
            b"\x05\x02\x00\x00\x00\x01\x00\x0a\x00HIVE"  # fake keytab header
        )
        b64_value = base64.b64encode(original_bytes).decode()

        result = await resolve_credential_file(
            b64_value, "keytab.keytab", str(tmp_path)
        )

        assert result == str(tmp_path / "keytab.keytab")
        assert os.path.exists(result)
        assert open(result, "rb").read() == original_bytes

    async def test_base64_text_file_written_correctly(self, tmp_path):
        """Valid base64 of a text file (krb5.conf) is decoded and written correctly."""
        krb5_content = b"[libdefaults]\n default_realm = EXAMPLE.COM\n"
        b64_value = base64.b64encode(krb5_content).decode()

        result = await resolve_credential_file(b64_value, "krb5.conf", str(tmp_path))

        assert result == str(tmp_path / "krb5.conf")
        assert open(result, "rb").read() == krb5_content

    async def test_base64_with_leading_trailing_whitespace(self, tmp_path):
        """Base64 string with surrounding whitespace is stripped before decoding."""
        content = b"fake-cert-bytes"
        b64_value = "  " + base64.b64encode(content).decode() + "\n"

        result = await resolve_credential_file(b64_value, "ca_cert.pem", str(tmp_path))

        assert result is not None
        assert open(result, "rb").read() == content

    async def test_base64_dest_dir_created_if_missing(self, tmp_path):
        """dest_dir is created automatically if it does not exist."""
        content = b"some-binary"
        b64_value = base64.b64encode(content).decode()
        new_dir = str(tmp_path / "nested" / "dir")

        result = await resolve_credential_file(b64_value, "file.bin", new_dir)

        assert result is not None
        assert os.path.isdir(new_dir)

    async def test_invalid_base64_returns_none(self, tmp_path):
        """A string that is neither JSON nor valid base64 returns None without raising."""
        result = await resolve_credential_file(
            "this is definitely not base64 !!!###", "keytab.keytab", str(tmp_path)
        )
        assert result is None

    async def test_strict_base64_rejects_non_alphabet_chars(self, tmp_path):
        """validate=True rejects strings with characters outside the base64 alphabet."""
        # length-correct but contains '!' — only caught with validate=True
        bad_value = "QUJDRA==" + "!!!!QUJD"  # 16 chars, length multiple of 4
        result = await resolve_credential_file(
            bad_value, "keytab.keytab", str(tmp_path)
        )
        assert result is None

    # ------------------------------------------------------------------
    # Customer object store path (objectstore:// prefix)
    # ------------------------------------------------------------------

    @patch(
        "application_sdk.credentials.utils.download_file",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.credentials.utils.create_store_from_binding")
    async def test_objectstore_prefix_downloads_via_deployment_binding(
        self, mock_create_store, mock_download, tmp_path
    ):
        """objectstore:// prefix routes to download_file with DEPLOYMENT binding."""
        fake_store = object()
        mock_create_store.return_value = fake_store

        result = await resolve_credential_file(
            "objectstore://kerberos/hiveadmin.keytab",
            "keytab.keytab",
            str(tmp_path),
        )

        # Binding name comes from the SDK constant
        from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME

        mock_create_store.assert_called_once_with(DEPLOYMENT_OBJECT_STORE_NAME)
        mock_download.assert_awaited_once_with(
            "kerberos/hiveadmin.keytab",
            os.path.join(str(tmp_path), "keytab.keytab"),
            store=fake_store,
        )
        assert result == os.path.join(str(tmp_path), "keytab.keytab")

    @patch(
        "application_sdk.credentials.utils.download_file",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.credentials.utils.create_store_from_binding")
    async def test_objectstore_prefix_strips_whitespace(
        self, mock_create_store, mock_download, tmp_path
    ):
        """Leading/trailing whitespace is stripped before prefix detection."""
        mock_create_store.return_value = object()

        result = await resolve_credential_file(
            "  objectstore://foo/bar.keytab  ",
            "keytab.keytab",
            str(tmp_path),
        )

        mock_download.assert_awaited_once()
        called_key = mock_download.await_args.args[0]
        assert called_key == "foo/bar.keytab"
        assert result == os.path.join(str(tmp_path), "keytab.keytab")

    async def test_objectstore_prefix_rejects_empty_key(self, tmp_path):
        """objectstore:// with no key after the prefix returns None."""
        result = await resolve_credential_file(
            "objectstore://", "keytab.keytab", str(tmp_path)
        )
        assert result is None

    async def test_objectstore_prefix_rejects_absolute_path(self, tmp_path):
        """Absolute paths after the prefix are rejected."""
        result = await resolve_credential_file(
            "objectstore:///etc/passwd", "keytab.keytab", str(tmp_path)
        )
        assert result is None

    async def test_objectstore_prefix_rejects_path_traversal(self, tmp_path):
        """Path traversal segments (..) are rejected."""
        result = await resolve_credential_file(
            "objectstore://kerberos/../secrets/keytab",
            "keytab.keytab",
            str(tmp_path),
        )
        assert result is None

    @patch(
        "application_sdk.credentials.utils.download_file",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.credentials.utils.create_store_from_binding")
    async def test_objectstore_download_failure_returns_none(
        self, mock_create_store, mock_download, tmp_path
    ):
        """Download failures are logged and return None — never raise."""
        mock_create_store.return_value = object()
        mock_download.side_effect = RuntimeError("network down")

        result = await resolve_credential_file(
            "objectstore://kerberos/hiveadmin.keytab",
            "keytab.keytab",
            str(tmp_path),
        )
        assert result is None

    # ------------------------------------------------------------------
    # Empty / None inputs
    # ------------------------------------------------------------------

    async def test_none_input_returns_none(self, tmp_path):
        """None input returns None immediately."""
        result = await resolve_credential_file(None, "keytab.keytab", str(tmp_path))
        assert result is None

    async def test_empty_string_returns_none(self, tmp_path):
        """Empty string returns None immediately."""
        result = await resolve_credential_file("", "keytab.keytab", str(tmp_path))
        assert result is None
