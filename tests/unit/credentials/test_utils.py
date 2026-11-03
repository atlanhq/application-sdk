import asyncio
import base64
import os
import warnings
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from application_sdk.credentials import utils as credential_utils
from application_sdk.credentials.errors import (
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.credentials.utils import (
    parse_credentials_extra,
    resolve_credential_file,
)
from application_sdk.handler.context import HandlerContext, bind_handler_context
from tests.unit.private_stdlib import private_stdlib


@pytest.fixture(autouse=True)
def _default_credential_base(tmp_path, monkeypatch):
    """Point the default credential base directory at the test's tmp_path."""
    monkeypatch.setattr(credential_utils, "_DEFAULT_CREDENTIAL_FILE_DIR", str(tmp_path))


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
        with patch("application_sdk.credentials.extra.logger") as mock_logger:
            assert parse_credentials_extra({"extra": extra}, strict=False) == {}

        mock_logger.warning.assert_called_once()
        message = mock_logger.warning.call_args.args[0]
        assert "extra" in message

    def test_lenient_drop_log_excludes_credential_material(self):
        """The reason is loggable; the value never is."""
        secret = "s3cr3t-token-value"
        with patch("application_sdk.credentials.extra.logger") as mock_logger:
            parse_credentials_extra(
                {"extra": f'{{"token": "{secret}", BROKEN'}, strict=False
            )

        rendered = " ".join(str(a) for a in mock_logger.warning.call_args.args)
        assert secret not in rendered

    def test_usable_extra_logs_nothing(self):
        """No warning on the happy path — this runs on every credential load."""
        with patch("application_sdk.credentials.extra.logger") as mock_logger:
            parse_credentials_extra({"extra": '{"host": "h"}'}, strict=False)
            parse_credentials_extra({"extra": {"host": "h"}}, strict=False)
            parse_credentials_extra({}, strict=False)
            parse_credentials_extra({"extra": None}, strict=False)

        mock_logger.warning.assert_not_called()


def _assert_in_call_dir(result: str | None, dest_dir: Any, filename: str) -> None:
    """*result* is a 0600 file at ``<dest_dir>/<per-call 0700 dir>/<filename>``.

    With a mocked download it is the empty name reservation.
    """
    assert result is not None
    call_dir = os.path.dirname(result)
    assert os.path.basename(result) == filename
    assert os.path.dirname(call_dir) == str(dest_dir)
    assert os.path.isdir(call_dir)
    if os.name == "posix":
        assert os.stat(call_dir).st_mode & 0o777 == 0o700
    assert os.path.isfile(result)
    if os.name == "posix":
        assert os.stat(result).st_mode & 0o777 == 0o600


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

        result = await resolve_credential_file(
            value, "keytab.keytab", raise_on_error=False
        )

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

        result = await resolve_credential_file(value, "krb5.conf", raise_on_error=False)

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
            b64_value, "keytab.keytab", raise_on_error=False
        )

        _assert_in_call_dir(result, tmp_path, "keytab.keytab")
        assert open(result, "rb").read() == original_bytes

    async def test_base64_text_file_written_correctly(self, tmp_path):
        """Valid base64 of a text file (krb5.conf) is decoded and written correctly."""
        krb5_content = b"[libdefaults]\n default_realm = EXAMPLE.COM\n"
        b64_value = base64.b64encode(krb5_content).decode()

        result = await resolve_credential_file(
            b64_value, "krb5.conf", raise_on_error=False
        )

        _assert_in_call_dir(result, tmp_path, "krb5.conf")
        assert open(result, "rb").read() == krb5_content

    async def test_base64_with_leading_trailing_whitespace(self, tmp_path):
        """Base64 string with surrounding whitespace is stripped before decoding."""
        content = b"fake-cert-bytes"
        b64_value = "  " + base64.b64encode(content).decode() + "\n"

        result = await resolve_credential_file(
            b64_value, "ca_cert.pem", raise_on_error=False
        )

        assert result is not None
        assert open(result, "rb").read() == content

    async def test_base64_dest_dir_created_if_missing(self, tmp_path):
        """A legacy plain-string dest_dir is created, and the file nests in it."""
        content = b"some-binary"
        b64_value = base64.b64encode(content).decode()
        new_dir = tmp_path / "nested" / "dir"

        with pytest.warns(DeprecationWarning, match="plain-string dest_dir"):
            result = await resolve_credential_file(
                b64_value, "file.bin", str(new_dir), raise_on_error=False
            )

        _assert_in_call_dir(result, new_dir, "file.bin")

    async def test_invalid_base64_returns_none(self, tmp_path):
        """A string that is neither JSON nor valid base64 returns None without raising."""
        result = await resolve_credential_file(
            "this is definitely not base64 !!!###",
            "keytab.keytab",
            raise_on_error=False,
        )
        assert result is None

    async def test_strict_base64_rejects_non_alphabet_chars(self, tmp_path):
        """validate=True rejects strings with characters outside the base64 alphabet."""
        # length-correct but contains '!' — only caught with validate=True
        bad_value = "QUJDRA==" + "!!!!QUJD"  # 16 chars, length multiple of 4
        result = await resolve_credential_file(
            bad_value, "keytab.keytab", raise_on_error=False
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
        """Without an infrastructure context, the DEPLOYMENT binding is built."""
        fake_store = object()
        mock_create_store.return_value = fake_store

        result = await resolve_credential_file(
            "objectstore://kerberos/hiveadmin.keytab",
            "keytab.keytab",
            raise_on_error=False,
        )

        # Binding name comes from the SDK constant
        from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME

        mock_create_store.assert_called_once_with(DEPLOYMENT_OBJECT_STORE_NAME)
        mock_download.assert_awaited_once_with(
            "kerberos/hiveadmin.keytab", result, store=fake_store
        )
        _assert_in_call_dir(result, tmp_path, "keytab.keytab")

    @patch(
        "application_sdk.storage.ops.download_file",
        new_callable=AsyncMock,
    )
    @patch("application_sdk.storage.binding.create_store_from_binding")
    async def test_objectstore_prefix_reuses_the_infrastructure_store(
        self, mock_create_store, mock_download, tmp_path
    ):
        """A started app's deployment store is reused, not rebuilt per call."""
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            clear_infrastructure,
            set_infrastructure,
        )

        deployment_store = object()
        set_infrastructure(InfrastructureContext(storage=deployment_store))  # type: ignore[arg-type]
        try:
            result = await resolve_credential_file(
                "objectstore://kerberos/krb5.conf",
                "krb5.conf",
                raise_on_error=True,
            )
        finally:
            clear_infrastructure()

        mock_create_store.assert_not_called()
        mock_download.assert_awaited_once_with(
            "kerberos/krb5.conf", result, store=deployment_store
        )

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
            raise_on_error=False,
        )

        mock_download.assert_awaited_once()
        called_key = mock_download.await_args.args[0]
        assert called_key == "foo/bar.keytab"
        _assert_in_call_dir(result, tmp_path, "keytab.keytab")

    async def test_objectstore_prefix_rejects_empty_key(self, tmp_path):
        """objectstore:// with no key after the prefix returns None."""
        result = await resolve_credential_file(
            "objectstore://", "keytab.keytab", raise_on_error=False
        )
        assert result is None

    async def test_objectstore_prefix_rejects_absolute_path(self, tmp_path):
        """Absolute paths after the prefix are rejected."""
        result = await resolve_credential_file(
            "objectstore:///etc/passwd",
            "keytab.keytab",
            raise_on_error=False,
        )
        assert result is None

    async def test_objectstore_prefix_rejects_path_traversal(self, tmp_path):
        """Path traversal segments (..) are rejected."""
        result = await resolve_credential_file(
            "objectstore://kerberos/../secrets/keytab",
            "keytab.keytab",
            raise_on_error=False,
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
            raise_on_error=False,
        )
        assert result is None
        # The per-call directory goes with the failure.
        assert os.listdir(tmp_path) == []

    # ------------------------------------------------------------------
    # Empty / None inputs
    # ------------------------------------------------------------------

    async def test_none_input_returns_none(self, tmp_path):
        """None input returns None immediately."""
        result = await resolve_credential_file(
            None, "keytab.keytab", raise_on_error=False
        )
        assert result is None

    async def test_empty_string_returns_none(self, tmp_path):
        """Empty string returns None immediately."""
        result = await resolve_credential_file(
            "", "keytab.keytab", raise_on_error=False
        )
        assert result is None


class TestResolveCredentialFileRaiseOnError:
    """raise_on_error=True: a failed resolution is never a silent None."""

    async def test_empty_value_raises_not_found(self, tmp_path):
        with pytest.raises(CredentialNotFoundError) as ei:
            await resolve_credential_file("", "ca_cert.pem", raise_on_error=True)
        assert ei.value.credential_name == "ca_cert.pem"

    async def test_invalid_base64_raises_parse_error_with_cause(self, tmp_path):
        with pytest.raises(CredentialParseError) as ei:
            await resolve_credential_file(
                "not base64 !!!", "keytab.keytab", raise_on_error=True
            )
        assert ei.value.cause is not None
        assert not os.path.exists(tmp_path / "keytab.keytab")

    @pytest.mark.parametrize(
        "value",
        ["objectstore://", "objectstore:///etc/passwd", "objectstore://a/../b"],
    )
    async def test_invalid_objectstore_key_raises_parse_error(self, value, tmp_path):
        with pytest.raises(CredentialParseError):
            await resolve_credential_file(value, "krb5.conf", raise_on_error=True)

    @patch(
        "application_sdk.storage.ops.download_file",
        new_callable=AsyncMock,
        side_effect=OSError("bucket unreachable"),
    )
    @patch("application_sdk.storage.binding.create_store_from_binding")
    async def test_objectstore_download_failure_propagates(
        self, mock_create_store, mock_download, tmp_path
    ):
        mock_create_store.return_value = object()
        with pytest.raises(OSError, match="bucket unreachable"):
            await resolve_credential_file(
                "objectstore://certs/ca.pem",
                "ca_cert.pem",
                raise_on_error=True,
            )

    async def test_valid_base64_returns_path(self, tmp_path):
        b64_value = base64.b64encode(b"cert").decode()
        result = await resolve_credential_file(
            b64_value, "ca_cert.pem", raise_on_error=True
        )
        assert open(result, "rb").read() == b"cert"


class TestResolveCredentialFileDeprecatedDefault:
    async def test_omitting_raise_on_error_warns(self, tmp_path):
        with pytest.warns(DeprecationWarning, match="v4.0"):
            result = await resolve_credential_file("", "ca_cert.pem")
        assert result is None

    async def test_explicit_false_does_not_warn(self, tmp_path):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await resolve_credential_file(
                "", "ca_cert.pem", raise_on_error=False
            )
        assert result is None


class TestResolveCredentialFileWhitespaceValue:
    @pytest.mark.parametrize("value", ["   ", "\n\t "])
    async def test_whitespace_only_raises_not_found(self, value, tmp_path):
        with pytest.raises(CredentialNotFoundError):
            await resolve_credential_file(value, "ca_cert.pem", raise_on_error=True)
        assert not os.path.exists(tmp_path / "ca_cert.pem")

    async def test_whitespace_only_is_none_in_legacy_mode(self, tmp_path):
        result = await resolve_credential_file(
            "   ", "ca_cert.pem", raise_on_error=False
        )
        assert result is None
        assert not os.path.exists(tmp_path / "ca_cert.pem")

    async def test_accepts_a_non_literal_bool(self, tmp_path):
        # A runtime-chosen ``bool`` behaves like the literal it holds. (The
        # overload side is not checked here: pyright excludes tests/.)
        strict: bool = bool(os.environ.get("_UNSET_FOR_THIS_TEST"))
        result: str | None = await resolve_credential_file(
            "", "ca_cert.pem", raise_on_error=strict
        )
        assert result is None


class TestResolveCredentialFilePerCallPaths:
    """FND-4275: one connection's credential file never lands on another's."""

    async def test_same_filename_gets_a_distinct_private_path_per_call(self, tmp_path):
        a = await resolve_credential_file(
            base64.b64encode(b"keytab-a").decode(),
            "keytab.keytab",
            raise_on_error=True,
        )
        b = await resolve_credential_file(
            base64.b64encode(b"keytab-b").decode(),
            "keytab.keytab",
            raise_on_error=True,
        )

        assert a != b
        _assert_in_call_dir(a, tmp_path, "keytab.keytab")
        _assert_in_call_dir(b, tmp_path, "keytab.keytab")
        assert open(a, "rb").read() == b"keytab-a"
        assert open(b, "rb").read() == b"keytab-b"

    async def test_no_partial_file_is_left_behind(self, tmp_path):
        result = await resolve_credential_file(
            base64.b64encode(b"cert").decode(),
            "ca_cert.pem",
            raise_on_error=True,
        )
        assert os.listdir(os.path.dirname(result)) == ["ca_cert.pem"]

    async def test_a_failed_write_removes_the_call_dir(self, tmp_path):
        with private_stdlib(credential_utils, "os") as local_os:
            local_os.replace = Mock(side_effect=OSError("disk full"))
            with pytest.raises(OSError, match="disk full"):
                await resolve_credential_file(
                    base64.b64encode(b"cert").decode(),
                    "ca_cert.pem",
                    raise_on_error=True,
                )
        assert os.listdir(tmp_path) == []

    async def test_outside_a_task_nothing_is_tracked(self, tmp_path):
        from application_sdk.app import base as app_base

        with app_base._credential_dirs_lock:
            before = {k: set(v) for k, v in app_base._credential_dirs_by_run.items()}
        result = await resolve_credential_file(
            base64.b64encode(b"cert").decode(),
            "ca_cert.pem",
            raise_on_error=True,
        )
        assert open(result, "rb").read() == b"cert"
        with app_base._credential_dirs_lock:
            assert app_base._credential_dirs_by_run == before

    @pytest.mark.parametrize(
        "filename",
        ["", ".", "..", "./../outside.key", "../outside.key", "sub/keytab", "/abs.key"],
    )
    @pytest.mark.parametrize(
        "value",
        [base64.b64encode(b"secret").decode(), "objectstore://kerberos/k.keytab"],
        ids=["base64", "objectstore"],
    )
    async def test_a_filename_that_is_a_path_is_rejected(
        self, filename, value, tmp_path
    ):
        with pytest.raises(CredentialParseError):
            await resolve_credential_file(value, filename, raise_on_error=True)
        # Nothing written: not in a call dir, and not beside dest_dir.
        assert os.listdir(tmp_path) == []
        assert not os.path.exists(tmp_path.parent / "outside.key")

    @pytest.mark.skipif(
        os.name != "nt", reason="backslash is a separator only on Windows"
    )
    async def test_a_backslash_filename_is_rejected_on_windows(self, tmp_path):
        with pytest.raises(CredentialParseError):
            await resolve_credential_file(
                base64.b64encode(b"secret").decode(),
                "..\\outside.key",
                raise_on_error=True,
            )


class TestCredentialDir:
    """One private directory holding several files (e.g. a SECUDIR)."""

    async def test_files_resolved_into_one_credential_dir_sit_side_by_side(
        self, tmp_path
    ):
        d = credential_utils.credential_dir()
        assert isinstance(d, credential_utils.CredentialDir)

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            pse = await resolve_credential_file(
                base64.b64encode(b"pse").decode(), "SAPSNCS.pse", d, raise_on_error=True
            )
            cred = await resolve_credential_file(
                base64.b64encode(b"cred").decode(), "cred_v2", d, raise_on_error=True
            )

        assert pse == os.path.join(d, "SAPSNCS.pse")
        assert cred == os.path.join(d, "cred_v2")
        assert sorted(os.listdir(d)) == ["SAPSNCS.pse", "cred_v2"]
        _assert_in_call_dir(pse, tmp_path, "SAPSNCS.pse")
        _assert_in_call_dir(cred, tmp_path, "cred_v2")

    async def test_each_credential_dir_is_new(self, tmp_path):
        assert credential_utils.credential_dir() != credential_utils.credential_dir()

    async def test_base_dir_is_honoured(self, tmp_path):
        base = tmp_path / "elsewhere"
        d = credential_utils.credential_dir(str(base))
        assert os.path.dirname(d) == str(base)

    async def test_a_second_file_of_the_same_name_is_refused(self, tmp_path):
        d = credential_utils.credential_dir()
        await resolve_credential_file(
            base64.b64encode(b"first").decode(), "keytab", d, raise_on_error=True
        )
        with pytest.raises(FileExistsError):
            await resolve_credential_file(
                base64.b64encode(b"second").decode(), "keytab", d, raise_on_error=True
            )
        assert open(os.path.join(d, "keytab"), "rb").read() == b"first"

    @patch("application_sdk.storage.ops.download_file", new_callable=AsyncMock)
    @patch("application_sdk.storage.binding.create_store_from_binding")
    async def test_objectstore_lands_in_the_credential_dir(
        self, mock_create_store, mock_download, tmp_path
    ):
        mock_create_store.return_value = object()
        d = credential_utils.credential_dir()
        result = await resolve_credential_file(
            "objectstore://kerberos/krb5.conf", "krb5.conf", d, raise_on_error=True
        )
        assert result == os.path.join(d, "krb5.conf")
        mock_download.assert_awaited_once_with(
            "kerberos/krb5.conf", result, store=mock_create_store.return_value
        )

    async def test_a_failed_write_keeps_the_callers_dir_and_its_files(self, tmp_path):
        d = credential_utils.credential_dir()
        await resolve_credential_file(
            base64.b64encode(b"pse").decode(), "SAPSNCS.pse", d, raise_on_error=True
        )
        with private_stdlib(credential_utils, "os") as local_os:
            local_os.replace = Mock(side_effect=OSError("disk full"))
            with pytest.raises(OSError, match="disk full"):
                await resolve_credential_file(
                    base64.b64encode(b"cred").decode(),
                    "cred_v2",
                    d,
                    raise_on_error=True,
                )
        # The caller's directory and its first file survive; no temp file left.
        assert os.listdir(d) == ["SAPSNCS.pse"]

    async def test_cannot_be_constructed_directly(self, tmp_path):
        with pytest.raises(TypeError, match="credential_dir"):
            credential_utils.CredentialDir(str(tmp_path))

    async def test_a_forged_credential_dir_around_a_shared_path_is_refused(
        self, tmp_path
    ):
        shared = tmp_path / "shared"
        shared.mkdir()
        forged = str.__new__(credential_utils.CredentialDir, str(shared))
        with pytest.raises(PermissionError, match="credential_dir"):
            await resolve_credential_file(
                base64.b64encode(b"secret").decode(),
                "keytab",
                forged,
                raise_on_error=True,
            )
        assert os.listdir(shared) == []

    async def test_a_private_directory_not_made_by_credential_dir_is_refused(
        self, tmp_path
    ):
        # Owner-only permission bits are not enough (on Windows they say
        # nothing at all): only a credential_dir() path passes.
        mine = tmp_path / "mine"
        mine.mkdir(mode=0o700)
        forged = str.__new__(credential_utils.CredentialDir, str(mine))
        with pytest.raises(PermissionError, match="credential_dir"):
            await resolve_credential_file(
                base64.b64encode(b"secret").decode(),
                "keytab",
                forged,
                raise_on_error=True,
            )
        assert os.listdir(mine) == []

    async def test_a_forged_credential_dir_around_an_issued_path_is_honoured(
        self, tmp_path
    ):
        # Same path as a real one: the same private directory, so no bypass.
        d = credential_utils.credential_dir()
        same = str.__new__(credential_utils.CredentialDir, str(d))
        path = await resolve_credential_file(
            base64.b64encode(b"k").decode(), "keytab", same, raise_on_error=True
        )
        assert path == os.path.join(d, "keytab")

    async def test_a_removed_credential_dir_recreated_at_its_path_is_refused(
        self, tmp_path
    ):
        ctx = HandlerContext(app_name="example-app")
        with bind_handler_context(ctx):
            d = credential_utils.credential_dir()
        assert not os.path.lexists(d)
        os.mkdir(d, 0o700)  # re-created afterwards, private but not the SDK's
        with pytest.raises(PermissionError):
            await resolve_credential_file(
                base64.b64encode(b"secret").decode(), "keytab", d, raise_on_error=True
            )
        assert os.listdir(d) == []

    @patch("application_sdk.storage.binding.create_store_from_binding")
    async def test_concurrent_resolutions_of_one_name_cannot_both_win(
        self, mock_create_store, tmp_path
    ):
        mock_create_store.return_value = object()
        d = credential_utils.credential_dir()
        release = asyncio.Event()
        published: list[bytes] = []

        async def slow_download(key: str, local_path: str, *, store: object) -> None:
            await release.wait()
            data = key.encode()
            with open(local_path, "wb") as f:
                f.write(data)
            published.append(data)

        with patch("application_sdk.storage.ops.download_file", new=slow_download):
            first = asyncio.ensure_future(
                resolve_credential_file(
                    "objectstore://a/krb5.conf", "krb5.conf", d, raise_on_error=True
                )
            )
            second = asyncio.ensure_future(
                resolve_credential_file(
                    "objectstore://b/krb5.conf", "krb5.conf", d, raise_on_error=True
                )
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)

        paths = [r for r in results if isinstance(r, str)]
        errors = [r for r in results if isinstance(r, BaseException)]
        assert len(paths) == 1 and len(errors) == 1
        assert isinstance(errors[0], FileExistsError)
        # Only the winner downloaded, so the file is the winner's content.
        assert len(published) == 1
        assert open(paths[0], "rb").read() == published[0]

    async def test_a_failed_write_releases_its_reserved_name(self, tmp_path):
        d = credential_utils.credential_dir()
        with private_stdlib(credential_utils, "os") as local_os:
            local_os.replace = Mock(side_effect=OSError("disk full"))
            with pytest.raises(OSError, match="disk full"):
                await resolve_credential_file(
                    base64.b64encode(b"x").decode(), "keytab", d, raise_on_error=True
                )
        # The name is free again for a retry.
        await resolve_credential_file(
            base64.b64encode(b"y").decode(), "keytab", d, raise_on_error=True
        )
        assert open(os.path.join(d, "keytab"), "rb").read() == b"y"


class TestHandlerScopedCleanup:
    """Outside a task, a handler invocation is the scope of its credential files."""

    async def test_files_made_in_a_handler_call_are_removed_when_it_returns(
        self, tmp_path
    ):
        ctx = HandlerContext(app_name="example-app")
        with bind_handler_context(ctx):
            path = await resolve_credential_file(
                base64.b64encode(b"ca").decode(), "ca.pem", raise_on_error=True
            )
            shared = credential_utils.credential_dir()
            assert os.path.isfile(path) and os.path.isdir(shared)
        assert not os.path.lexists(os.path.dirname(path))
        assert not os.path.lexists(shared)
        assert ctx._credential_dirs == {}

    async def test_repeated_handler_calls_do_not_accumulate_directories(self, tmp_path):
        for _ in range(3):
            with bind_handler_context(HandlerContext(app_name="example-app")):
                await resolve_credential_file(
                    base64.b64encode(b"ca").decode(), "ca.pem", raise_on_error=True
                )
        assert [e for e in os.listdir(tmp_path) if e.startswith("cred-")] == []

    async def test_a_directory_that_cannot_be_removed_is_retried_on_a_later_exit(
        self, tmp_path
    ):
        from application_sdk.handler import context as handler_context

        with private_stdlib(handler_context, "shutil") as local_shutil:
            local_shutil.rmtree = Mock()  # removal fails silently
            with bind_handler_context(HandlerContext(app_name="example-app")):
                path = await resolve_credential_file(
                    base64.b64encode(b"ca").decode(), "ca.pem", raise_on_error=True
                )
        stuck = os.path.dirname(path)
        assert os.path.isdir(stuck)
        assert stuck in handler_context._pending_credential_dirs
        assert stuck in credential_utils._issued_dirs

        # A later, unrelated handler invocation's exit removes it.
        with bind_handler_context(HandlerContext(app_name="example-app")):
            pass
        assert not os.path.lexists(stuck)
        assert stuck not in handler_context._pending_credential_dirs
        assert stuck not in credential_utils._issued_dirs
