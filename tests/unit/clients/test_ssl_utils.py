"""Tests for SSL utilities."""

import socket
import ssl
import tempfile
from unittest.mock import patch

import aiohttp
import httpx
import pytest
import trustme
from aiohttp import web

from application_sdk.clients.ssl_utils import (
    create_ssl_context_with_custom_certs,
    get_ssl_cert_dir,
    get_ssl_context,
)


def has_internet_connection() -> bool:
    """Check if there is an active internet connection."""
    try:
        conn = socket.create_connection(("www.google.com", 443), timeout=3)
        conn.close()
        return True
    except Exception:
        return False


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
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "application_sdk.clients.ssl_utils.get_ssl_cert_dir",
                return_value=tmpdir,
            ):
                result = get_ssl_context()
                assert isinstance(result, ssl.SSLContext)

    def test_integration_with_real_directory(self):
        """Integration test with real temporary directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                result = get_ssl_context()
                assert isinstance(result, ssl.SSLContext)


class TestSslContextPublicCertificates:
    """Test that SSL context works with public certificates (default CAs).

    These tests verify that when custom certificates are added, the default
    system certificates are still available, allowing connections to public
    services like Google.
    """

    @pytest.mark.asyncio
    async def test_httpx_ssl_context_works_with_public_url(self):
        """Test that httpx with custom SSL context can connect to public HTTPS URLs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                ssl_context = get_ssl_context()
                assert isinstance(ssl_context, ssl.SSLContext)

                if has_internet_connection():
                    async with httpx.AsyncClient(verify=ssl_context) as client:
                        response = await client.get(
                            "https://www.google.com", timeout=10
                        )
                        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_aiohttp_ssl_context_works_with_public_url(self):
        """Test that aiohttp with custom SSL context can connect to public HTTPS URLs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                ssl_context = get_ssl_context()
                assert isinstance(ssl_context, ssl.SSLContext)

                if has_internet_connection():
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            "https://www.google.com",
                            ssl=ssl_context,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as response:
                            assert response.status == 200

    @pytest.mark.asyncio
    async def test_default_ssl_works_with_public_url(self):
        """Test that default SSL (no custom certs) works with public URLs."""
        with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", ""):
            ssl_context = get_ssl_context()
            assert ssl_context is True

            if has_internet_connection():
                async with httpx.AsyncClient(verify=ssl_context) as client:
                    response = await client.get("https://www.google.com", timeout=10)
                    assert response.status_code == 200


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


@pytest.fixture
def trustme_ca():  # type: ignore[no-untyped-def]
    """Create a trustme CA for testing."""
    return trustme.CA()


@pytest.fixture
def server_ssl_ctx(trustme_ca):  # type: ignore[no-untyped-def]
    """Create a server SSL context with a certificate from the trustme CA."""
    server_ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    trustme_ca.issue_cert("localhost", "127.0.0.1").configure_cert(server_ssl_ctx)  # type: ignore[no-untyped-call]
    return server_ssl_ctx


class TestSslContextWithPrivateServer:
    """Integration tests with a real HTTPS server using private certificates."""

    @pytest.mark.asyncio
    async def test_httpx_connects_to_private_https_server(
        self, trustme_ca, server_ssl_ctx
    ):  # type: ignore[no-untyped-def]
        """Test that httpx can connect to an HTTPS server using a private CA certificate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ca_cert_path = f"{tmpdir}/ca.pem"
            trustme_ca.cert_pem.write_to_path(ca_cert_path)  # type: ignore[no-untyped-call]

            async def handle_request(request):  # type: ignore[no-untyped-def]
                return web.Response(text="Hello from private HTTPS server!")

            app = web.Application()
            app.router.add_get("/", handle_request)

            runner = web.AppRunner(app)
            await runner.setup()

            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl_ctx)
            await site.start()

            port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

            try:
                with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                    client_ssl_context = get_ssl_context()
                    assert isinstance(client_ssl_context, ssl.SSLContext)

                    async with httpx.AsyncClient(verify=client_ssl_context) as client:
                        response = await client.get(
                            f"https://localhost:{port}/", timeout=10
                        )
                        assert response.status_code == 200
                        assert response.text == "Hello from private HTTPS server!"
            finally:
                await runner.cleanup()

    @pytest.mark.asyncio
    async def test_aiohttp_connects_to_private_https_server(
        self, trustme_ca, server_ssl_ctx
    ):  # type: ignore[no-untyped-def]
        """Test that aiohttp can connect to an HTTPS server using a private CA certificate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ca_cert_path = f"{tmpdir}/ca.pem"
            trustme_ca.cert_pem.write_to_path(ca_cert_path)  # type: ignore[no-untyped-call]

            async def handle_request(request):  # type: ignore[no-untyped-def]
                return web.Response(text="Hello from private HTTPS server!")

            app = web.Application()
            app.router.add_get("/", handle_request)

            runner = web.AppRunner(app)
            await runner.setup()

            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl_ctx)
            await site.start()

            port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

            try:
                with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                    client_ssl_context = get_ssl_context()
                    assert isinstance(client_ssl_context, ssl.SSLContext)

                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f"https://localhost:{port}/",
                            ssl=client_ssl_context,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as response:
                            assert response.status == 200
                            text = await response.text()
                            assert text == "Hello from private HTTPS server!"
            finally:
                await runner.cleanup()

    @pytest.mark.asyncio
    async def test_connection_fails_without_custom_ca(self, trustme_ca, server_ssl_ctx):  # type: ignore[no-untyped-def]
        """Test that connection fails when custom CA is not loaded."""

        async def handle_request(request):  # type: ignore[no-untyped-def]
            return web.Response(text="Hello!")

        app = web.Application()
        app.router.add_get("/", handle_request)

        runner = web.AppRunner(app)
        await runner.setup()

        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl_ctx)
        await site.start()

        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        try:
            with pytest.raises(Exception) as exc_info:
                async with httpx.AsyncClient(verify=True) as client:
                    await client.get(f"https://localhost:{port}/", timeout=10)

            error_message = str(exc_info.value).lower()
            assert "certificate" in error_message or "ssl" in error_message
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_private_and_public_certs_work_simultaneously(
        self, trustme_ca, server_ssl_ctx
    ):  # type: ignore[no-untyped-def]
        """Test that both private server AND public URLs work with the same SSL context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ca_cert_path = f"{tmpdir}/ca.pem"
            trustme_ca.cert_pem.write_to_path(ca_cert_path)  # type: ignore[no-untyped-call]

            async def handle_request(request):  # type: ignore[no-untyped-def]
                return web.Response(text="Private server response")

            app = web.Application()
            app.router.add_get("/", handle_request)

            runner = web.AppRunner(app)
            await runner.setup()

            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ssl_ctx)
            await site.start()

            port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

            try:
                with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                    client_ssl_context = get_ssl_context()
                    assert isinstance(client_ssl_context, ssl.SSLContext)

                    async with httpx.AsyncClient(verify=client_ssl_context) as client:
                        private_response = await client.get(
                            f"https://localhost:{port}/", timeout=10
                        )
                        assert private_response.status_code == 200
                        assert private_response.text == "Private server response"

                        if has_internet_connection():
                            public_response = await client.get(
                                "https://www.google.com", timeout=10
                            )
                            assert public_response.status_code == 200
            finally:
                await runner.cleanup()
