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
