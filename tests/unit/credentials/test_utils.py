import base64
import os
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.credentials.errors import CredentialParseError
from application_sdk.credentials.utils import (
    parse_credentials_extra,
    resolve_credential_file,
)


class TestParseCredentialsExtra:
    """The single decoder for the credential ``extra`` field.

    ``extra`` arrives in two legal shapes — a nested object, or that object
    serialized to a JSON string — because its producers straddle the v2/v3
    contract boundary. Every consumer routes through this function precisely
    so a shape one caller accepts cannot be a shape another caller drops.

    Two policies, one decoder. ``strict=True`` (runtime clients) fails loudly:
    a connector cannot build a DSN from an unusable ``extra``, and a typed
    error beats an ``AttributeError`` three frames later. ``strict=False``
    (the flattener) returns ``{}``: it runs where no caller can distinguish
    a malformed credential from an absent one, so it must not raise.
    """

    # -- shapes accepted identically in both modes --------------------------

    @pytest.mark.parametrize("strict", [True, False])
    def test_dict_extra_returned_as_is(self, strict):
        extra = {"host": "h", "port": 1521}
        assert parse_credentials_extra({"extra": extra}, strict=strict) == extra

    @pytest.mark.parametrize("strict", [True, False])
    def test_json_string_extra_decoded(self, strict):
        assert parse_credentials_extra(
            {"extra": '{"host": "h", "port": 1521}'}, strict=strict
        ) == {"host": "h", "port": 1521}

    @pytest.mark.parametrize("strict", [True, False])
    def test_both_shapes_decode_identically(self, strict):
        """The parity that the whole two-shape tolerance exists to provide."""
        as_dict = parse_credentials_extra(
            {"extra": {"host": "h", "sid": "DB1"}}, strict=strict
        )
        as_string = parse_credentials_extra(
            {"extra": '{"host": "h", "sid": "DB1"}'}, strict=strict
        )
        assert as_string == as_dict

    @pytest.mark.parametrize("strict", [True, False])
    def test_absent_extra_is_empty_dict(self, strict):
        assert parse_credentials_extra({"username": "u"}, strict=strict) == {}

    @pytest.mark.parametrize("strict", [True, False])
    def test_null_extra_is_empty_dict(self, strict):
        """An explicit JSON null is 'no extra', not a value to hand back.

        Returning ``None`` here handed every caller an ``AttributeError`` on
        the next ``.get()``.
        """
        assert parse_credentials_extra({"extra": None}, strict=strict) == {}

    @pytest.mark.parametrize("strict", [True, False])
    def test_empty_string_extra_is_empty_dict(self, strict):
        assert parse_credentials_extra({"extra": ""}, strict=strict) == {}

    # -- where the two policies diverge -------------------------------------

    def test_strict_raises_on_undecodable_string(self):
        with pytest.raises(CredentialParseError) as exc_info:
            parse_credentials_extra({"extra": "{not-json"})
        assert exc_info.value.credential_name == "extra"

    def test_lenient_returns_empty_on_undecodable_string(self):
        assert parse_credentials_extra({"extra": "{not-json"}, strict=False) == {}

    def test_strict_raises_on_non_object_json(self):
        """A JSON array decodes cleanly but is not an object.

        The declared return type is ``dict``; handing back a list produced an
        ``AttributeError`` in the caller instead of a typed credential error.
        """
        with pytest.raises(CredentialParseError):
            parse_credentials_extra({"extra": '["a", "b"]'})

    def test_lenient_returns_empty_on_non_object_json(self):
        assert parse_credentials_extra({"extra": '["a", "b"]'}, strict=False) == {}

    def test_strict_raises_on_non_mapping_value(self):
        with pytest.raises(CredentialParseError):
            parse_credentials_extra({"extra": 7})

    def test_lenient_returns_empty_on_non_mapping_value(self):
        assert parse_credentials_extra({"extra": 7}, strict=False) == {}

    def test_strict_is_the_default(self):
        """Callers that omit the flag get the loud policy."""
        with pytest.raises(CredentialParseError):
            parse_credentials_extra({"extra": "{not-json"})

    # -- the lenient path must never be silent ------------------------------

    @pytest.mark.parametrize(
        "extra",
        ["{not-json", '["a", "b"]', 7],
        ids=["undecodable", "json_array", "non_mapping"],
    )
    def test_lenient_drop_is_logged(self, extra):
        """A swallowed `extra` leaves a trace or the next defect is invisible.

        Silence is what let a dropped `extra` reach production: the credential
        looked complete, the connectivity check just failed. The lenient caller
        has no exception to surface, so this log line is the only signal.
        """
        with patch("application_sdk.credentials.utils.logger") as mock_logger:
            assert parse_credentials_extra({"extra": extra}, strict=False) == {}

        mock_logger.warning.assert_called_once()
        message = mock_logger.warning.call_args.args[0]
        assert "extra" in message

    def test_lenient_drop_log_excludes_credential_material(self):
        """The reason is loggable; the value never is."""
        secret = "s3cr3t-token-value"
        with patch("application_sdk.credentials.utils.logger") as mock_logger:
            parse_credentials_extra(
                {"extra": f'{{"token": "{secret}", BROKEN'}, strict=False
            )

        rendered = " ".join(str(a) for a in mock_logger.warning.call_args.args)
        assert secret not in rendered

    def test_usable_extra_logs_nothing(self):
        """No warning on the happy path — this runs on every credential load."""
        with patch("application_sdk.credentials.utils.logger") as mock_logger:
            parse_credentials_extra({"extra": '{"host": "h"}'}, strict=False)
            parse_credentials_extra({"extra": {"host": "h"}}, strict=False)
            parse_credentials_extra({}, strict=False)
            parse_credentials_extra({"extra": None}, strict=False)

        mock_logger.warning.assert_not_called()


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
        "application_sdk.storage.ops.download_file",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.storage.binding.create_store_from_binding")
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
        "application_sdk.storage.ops.download_file",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.storage.binding.create_store_from_binding")
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
        "application_sdk.storage.ops.download_file",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.storage.binding.create_store_from_binding")
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
