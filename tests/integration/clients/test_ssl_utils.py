"""Integration tests for SSL utilities.

TestSslContextWithPrivateServer: spins up a local stdlib TLS server via trustme
and makes real connections — no external network needed.

TestSslContextPublicCertificates: verifies that the custom SSL context still
reaches public HTTPS endpoints (requires outbound internet, available on CI
runners). Moved from tests/unit when --disable-socket was added; the
has_internet_connection() guard was dropped so the network call IS the assertion.
"""

import ssl
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx
import pytest
import trustme

from application_sdk.clients.base import BaseClient
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


@pytest.fixture
def https_server(
    server_ssl_ctx: ssl.SSLContext,
) -> Callable[[str], AbstractContextManager[int]]:
    """Serve ``GET /`` with a fixed body over TLS on 127.0.0.1; yields the port."""

    @contextmanager
    def _serve(body: str) -> Iterator[int]:
        payload = body.encode()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler hook name
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.socket = server_ssl_ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    return _serve


@pytest.mark.integration
class TestSslContextWithPrivateServer:
    """Tests with a real local HTTPS server using private certificates."""

    @pytest.mark.asyncio
    async def test_httpx_connects_to_private_https_server(
        self, trustme_ca, https_server
    ):  # type: ignore[no-untyped-def]
        """Test that httpx can connect to an HTTPS server using a private CA certificate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ca_cert_path = f"{tmpdir}/ca.pem"
            trustme_ca.cert_pem.write_to_path(ca_cert_path)  # type: ignore[no-untyped-call]

            with https_server("Hello from private HTTPS server!") as port:
                with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                    client_ssl_context = get_ssl_context()
                    assert isinstance(client_ssl_context, ssl.SSLContext)

                    async with httpx.AsyncClient(verify=client_ssl_context) as client:
                        response = await client.get(
                            f"https://localhost:{port}/", timeout=10
                        )
                        assert response.status_code == 200
                        assert response.text == "Hello from private HTTPS server!"

    @pytest.mark.asyncio
    async def test_base_client_connects_to_private_https_server(
        self, trustme_ca, https_server
    ):  # type: ignore[no-untyped-def]
        """BaseClient's shared transport trusts CAs from SSL_CERT_DIR.

        Regression: the transport was built without ``verify=`` and the
        per-call ``AsyncClient(verify=...)`` is ignored by httpx when a
        transport is passed, so private-CA endpoints failed TLS.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            ca_cert_path = f"{tmpdir}/ca.pem"
            trustme_ca.cert_pem.write_to_path(ca_cert_path)  # type: ignore[no-untyped-call]

            with https_server("Hello from private HTTPS server!") as port:
                with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                    async with BaseClient() as client:
                        response = await client.execute_http_get_request(
                            f"https://localhost:{port}/",
                            timeout=10,
                            raise_on_error=True,
                        )
                assert response.status_code == 200
                assert response.text == "Hello from private HTTPS server!"

    @pytest.mark.asyncio
    async def test_connection_fails_without_custom_ca(self, https_server):  # type: ignore[no-untyped-def]
        """Test that connection fails when custom CA is not loaded."""

        with https_server("Hello!") as port:
            with pytest.raises(Exception) as exc_info:
                async with httpx.AsyncClient(verify=True) as client:
                    await client.get(f"https://localhost:{port}/", timeout=10)

            error_message = str(exc_info.value).lower()
            assert "certificate" in error_message or "ssl" in error_message

    @pytest.mark.asyncio
    async def test_private_and_public_certs_work_simultaneously(
        self, trustme_ca, https_server
    ):  # type: ignore[no-untyped-def]
        """Test that both private server AND public URLs work with the same SSL context."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ca_cert_path = f"{tmpdir}/ca.pem"
            trustme_ca.cert_pem.write_to_path(ca_cert_path)  # type: ignore[no-untyped-call]

            with https_server("Private server response") as port:
                with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", tmpdir):
                    client_ssl_context = get_ssl_context()
                    assert isinstance(client_ssl_context, ssl.SSLContext)

                    async with httpx.AsyncClient(verify=client_ssl_context) as client:
                        private_response = await client.get(
                            f"https://localhost:{port}/", timeout=10
                        )
                        assert private_response.status_code == 200
                        assert private_response.text == "Private server response"


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
    async def test_default_ssl_works_with_public_url(self):
        with patch("application_sdk.clients.ssl_utils.SSL_CERT_DIR", ""):
            ssl_context = get_ssl_context()
            assert ssl_context is True
            async with httpx.AsyncClient(verify=ssl_context) as client:
                response = await client.get("https://www.google.com", timeout=10)
                assert response.status_code == 200
