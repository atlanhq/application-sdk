"""Tests for SSL utilities."""

import os
import ssl
import tempfile
from unittest.mock import patch

import pytest
import trustme

from application_sdk.clients.ssl_utils import (
    create_ssl_context_with_custom_certs,
    get_ssl_cert_dir,
    get_ssl_context,
)


@pytest.fixture(autouse=True)
def clear_ssl_context_cache():
    """Clear the get_ssl_context lru_cache before and after each test."""
    get_ssl_context.cache_clear()
    yield
    get_ssl_context.cache_clear()


class TestGetSslCertDir:
    """Test cases for get_ssl_cert_dir function."""

    def test_returns_none_when_not_set(self):
        """Test that None is returned when SSL_CERT_DIR is not set."""
        with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", ""):
            result = get_ssl_cert_dir()
            assert result is None

    def test_returns_none_when_dir_not_exists(self):
        """Test that None is returned when SSL_CERT_DIR points to non-existent directory."""
        with patch(
            "application_sdk.clients.ssl_utils.SSL_CERT_DIR",
            "/nonexistent/path/to/certs",
        ):
            result = get_ssl_cert_dir()
            assert result is None

    def test_returns_path_when_dir_exists(self):
        """Test that path is returned when SSL_CERT_DIR points to existing directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                result = get_ssl_cert_dir()
                assert result == tmpdir


class TestCreateSslContextWithCustomCerts:
    """Test cases for create_ssl_context_with_custom_certs function."""

    def test_returns_ssl_context(self):
        """Test that an SSLContext is returned."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = create_ssl_context_with_custom_certs(tmpdir)
            assert isinstance(result, ssl.SSLContext)

    def test_ssl_context_has_default_verify_mode(self):
        """Test that the SSL context has certificate verification enabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = create_ssl_context_with_custom_certs(tmpdir)
            assert result.verify_mode == ssl.CERT_REQUIRED

    def test_ssl_context_checks_hostname(self):
        """Test that the SSL context checks hostname by default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = create_ssl_context_with_custom_certs(tmpdir)
            assert result.check_hostname is True


class TestGetSslContext:
    """Test cases for get_ssl_context function."""

    def test_returns_true_when_no_cert_dir(self):
        """Test that True is returned when no SSL_CERT_DIR is set."""
        with patch(
            "application_sdk.clients.ssl_utils.get_ssl_cert_dir", return_value=None
        ):
            result = get_ssl_context()
            assert result is True

    def test_returns_ssl_context_when_cert_dir_set(self):
        """Test that SSLContext is returned when SSL_CERT_DIR is set."""
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch(
                "application_sdk.clients.ssl_utils.get_ssl_cert_dir",
                return_value=tmpdir,
            ),
        ):
            result = get_ssl_context()
            assert isinstance(result, ssl.SSLContext)

    def test_integration_with_real_directory(self):
        """Integration test with real temporary directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                result = get_ssl_context()
                assert isinstance(result, ssl.SSLContext)


class TestSslContextPrivateCertificates:
    """Test that SSL context can load and use private/custom certificates."""

    def test_ssl_context_loads_custom_cert_file(self):
        """Test that a custom certificate file can be loaded into the SSL context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cert_file = f"{tmpdir}/custom-ca.pem"
            with open(cert_file, "w") as f:
                f.write("# Placeholder for custom CA certificate\n")

            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                ssl_context = get_ssl_context()
                assert isinstance(ssl_context, ssl.SSLContext)

    def test_ssl_context_created_with_verification_enabled(self):
        """Test that SSL context has proper verification settings for security."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ssl_context = create_ssl_context_with_custom_certs(tmpdir)

            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert ssl_context.check_hostname is True
            assert ssl_context.protocol == ssl.PROTOCOL_TLS_CLIENT


class TestGetCustomCaCertBytes:
    """Test cases for get_custom_ca_cert_bytes function."""

    def test_returns_none_when_no_cert_dir(self):
        """Test that None is returned when SSL_CERT_DIR is not set."""
        with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", ""):
            from application_sdk.clients.ssl_utils import get_custom_ca_cert_bytes

            result = get_custom_ca_cert_bytes()
            assert result is None

    def test_returns_none_when_cert_dir_not_exists(self):
        """Test that None is returned when SSL_CERT_DIR points to non-existent directory."""
        with patch(
            "application_sdk.clients.ssl_utils.SSL_CERT_DIR",
            "/nonexistent/path/to/certs",
        ):
            from application_sdk.clients.ssl_utils import get_custom_ca_cert_bytes

            result = get_custom_ca_cert_bytes()
            assert result is None

    def test_returns_none_when_cert_dir_empty(self):
        """Test that None is returned when SSL_CERT_DIR contains no certificate files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                from application_sdk.clients.ssl_utils import get_custom_ca_cert_bytes

                result = get_custom_ca_cert_bytes()
                assert result is None

    def test_includes_custom_cert_when_cert_file_exists(self):
        """Test that custom certificate bytes are included when a valid cert file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cert_content = b"-----BEGIN CERTIFICATE-----\nUNIQUE_TEST_CERT_DATA_12345\n-----END CERTIFICATE-----"
            cert_file = f"{tmpdir}/ca.pem"
            with open(cert_file, "wb") as f:
                f.write(cert_content)

            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                from application_sdk.clients.ssl_utils import get_custom_ca_cert_bytes

                result = get_custom_ca_cert_bytes()
                assert result is not None
                assert b"UNIQUE_TEST_CERT_DATA_12345" in result

    def test_includes_default_system_certificates(self):
        """Test that default system CA certificates are included along with custom certs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_cert = (
                b"-----BEGIN CERTIFICATE-----\nCUSTOM_CERT\n-----END CERTIFICATE-----"
            )
            cert_file = f"{tmpdir}/custom-ca.pem"
            with open(cert_file, "wb") as f:
                f.write(custom_cert)

            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                from application_sdk.clients.ssl_utils import get_custom_ca_cert_bytes

                result = get_custom_ca_cert_bytes()
                assert result is not None
                assert b"CUSTOM_CERT" in result
                assert len(result) > len(custom_cert)
                assert result.count(b"-----BEGIN CERTIFICATE-----") > 1

    def test_concatenates_multiple_custom_cert_files(self):
        """Test that multiple custom certificate files are concatenated with newlines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cert1_content = b"-----BEGIN CERTIFICATE-----\nCUSTOM_CERT_ONE\n-----END CERTIFICATE-----"
            cert2_content = b"-----BEGIN CERTIFICATE-----\nCUSTOM_CERT_TWO\n-----END CERTIFICATE-----"

            with open(f"{tmpdir}/ca1.pem", "wb") as f:
                f.write(cert1_content)
            with open(f"{tmpdir}/ca2.crt", "wb") as f:
                f.write(cert2_content)

            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                from application_sdk.clients.ssl_utils import get_custom_ca_cert_bytes

                result = get_custom_ca_cert_bytes()
                assert result is not None
                assert b"CUSTOM_CERT_ONE" in result
                assert b"CUSTOM_CERT_TWO" in result

    def test_returns_bytes_with_trustme_certificate(self):
        """Test with a real trustme-generated certificate."""
        ca = trustme.CA()

        with tempfile.TemporaryDirectory() as tmpdir:
            ca_cert_path = f"{tmpdir}/ca.pem"
            ca.cert_pem.write_to_path(ca_cert_path)  # type: ignore[no-untyped-call]

            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                from application_sdk.clients.ssl_utils import get_custom_ca_cert_bytes

                result = get_custom_ca_cert_bytes()
                assert result is not None
                assert b"-----BEGIN CERTIFICATE-----" in result
                assert b"-----END CERTIFICATE-----" in result
                assert result.count(b"-----BEGIN CERTIFICATE-----") > 1


class TestConfigureRequestsCaBundle:
    """Test cases for configure_requests_ca_bundle function."""

    def test_noop_when_no_cert_dir(self, monkeypatch):
        """Returns None and leaves env untouched when SSL_CERT_DIR is unset."""
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
        with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", ""):
            from application_sdk.clients.ssl_utils import configure_requests_ca_bundle

            result = configure_requests_ca_bundle()
            assert result is None
            assert "REQUESTS_CA_BUNDLE" not in os.environ

    def test_respects_existing_override(self, monkeypatch):
        """An operator-set REQUESTS_CA_BUNDLE is left untouched."""
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/operator/set/bundle.pem")
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(f"{tmpdir}/ca.pem", "wb") as f:
                f.write(b"-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----")
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                from application_sdk.clients.ssl_utils import (
                    configure_requests_ca_bundle,
                )

                result = configure_requests_ca_bundle()
                assert result == "/operator/set/bundle.pem"
                assert os.environ["REQUESTS_CA_BUNDLE"] == "/operator/set/bundle.pem"

    def test_exports_combined_bundle_when_cert_dir_set(self, monkeypatch):
        """Writes the combined default+custom bundle and exports the env vars."""
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_cert = (
                b"-----BEGIN CERTIFICATE-----\nCUSTOM_ROOT\n-----END CERTIFICATE-----"
            )
            with open(f"{tmpdir}/ca.pem", "wb") as f:
                f.write(custom_cert)
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                from application_sdk.clients.ssl_utils import (
                    configure_requests_ca_bundle,
                )

                result = configure_requests_ca_bundle()
                assert result is not None
                assert os.environ["REQUESTS_CA_BUNDLE"] == result
                assert os.environ["CURL_CA_BUNDLE"] == result
                with open(result, "rb") as f:
                    written = f.read()
                # Custom root present AND combined with system defaults.
                assert b"CUSTOM_ROOT" in written
                assert written.count(b"-----BEGIN CERTIFICATE-----") > 1

    def test_idempotent(self, monkeypatch):
        """A second call reuses the already-exported bundle path."""
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(f"{tmpdir}/ca.pem", "wb") as f:
                f.write(b"-----BEGIN CERTIFICATE-----\nY\n-----END CERTIFICATE-----")
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                from application_sdk.clients.ssl_utils import (
                    configure_requests_ca_bundle,
                )

                first = configure_requests_ca_bundle()
                second = configure_requests_ca_bundle()
                assert first is not None
                assert first == second
