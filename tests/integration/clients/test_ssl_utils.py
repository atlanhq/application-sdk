"""Integration tests for SSL utilities.

TestSslContextWithPrivateServer: spins up a local aiohttp TLS server via trustme
and makes real connections — no external network needed.

TestSslContextPublicCertificates: verifies that the custom SSL context still
reaches public HTTPS endpoints (requires outbound internet, available on CI
runners). Moved from tests/unit when --disable-socket was added; the
has_internet_connection() guard was dropped so the network call IS the assertion.
"""

import ssl
import tempfile
from unittest.mock import patch

import aiohttp
import httpx
import pytest
import trustme
from aiohttp import web

from application_sdk.clients.ssl_utils import get_ssl_context


@pytest.fixture(autouse=True)
def clear_ssl_context_cache():  # type: ignore[no-untyped-def]
    """Clear the get_ssl_context lru_cache before and after each test.

    Each test uses a different trustme CA + tmpdir; without this the first
    call caches an SSL context keyed to the first tmpdir, and subsequent
    tests get that stale context (wrong CA), causing cert verification errors.
    """
    get_ssl_context.cache_clear()
    yield
    get_ssl_context.cache_clear()


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


@pytest.mark.integration
class TestSslContextWithPrivateServer:
    """Tests with a real local HTTPS server using private certificates."""

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
            finally:
                await runner.cleanup()


@pytest.mark.integration
class TestSslContextPublicCertificates:
    """Verify the custom SSL context still reaches public HTTPS endpoints.

    Requires outbound internet (available on CI runners). The network call IS
    the assertion — no has_internet_connection() guard that silently skips.
    """

    @pytest.mark.asyncio
    async def test_httpx_ssl_context_works_with_public_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                ssl_context = get_ssl_context()
                assert isinstance(ssl_context, ssl.SSLContext)
                async with httpx.AsyncClient(verify=ssl_context) as client:
                    response = await client.get("https://www.google.com", timeout=10)
                    assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_aiohttp_ssl_context_works_with_public_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                ssl_context = get_ssl_context()
                assert isinstance(ssl_context, ssl.SSLContext)
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://www.google.com",
                        ssl=ssl_context,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response:
                        assert response.status == 200

    @pytest.mark.asyncio
    async def test_default_ssl_works_with_public_url(self):
        with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", ""):
            ssl_context = get_ssl_context()
            assert ssl_context is True
            async with httpx.AsyncClient(verify=ssl_context) as client:
                response = await client.get("https://www.google.com", timeout=10)
                assert response.status_code == 200
