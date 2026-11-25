"""BaseClient's default transport honors HTTP(S)_PROXY / ALL_PROXY / NO_PROXY.

httpx disables its own environment-proxy handling whenever ``transport=`` is
passed, which ``BaseClient`` always does, so the default transport has to
apply the proxy itself. Without it, hosts whose egress is proxy-only time out.
"""

from typing import Any

import httpcore
import httpx
import pytest

from application_sdk.clients.base import BaseClient, _EnvProxyHTTPTransport

_PROXY_VARS = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
_PROXY = "http://proxy.internal:3128"


class _LoadedClient(BaseClient):
    async def load(self, **kwargs: Any) -> None:
        return None


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class _FakeProxiedTransport:
    """Stands in for the lazily built proxied transport; records its kwargs."""

    instances: list["_FakeProxiedTransport"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.requests: list[httpx.Request] = []
        self.closed = False
        type(self).instances.append(self)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, request=request)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_proxied():
    _FakeProxiedTransport.instances = []
    return _FakeProxiedTransport


def _client(fake: type[_FakeProxiedTransport]) -> _LoadedClient:
    """A client whose proxied transports are fakes (patched on the instance, so
    nothing global changes under xdist)."""
    client = _LoadedClient()
    transport = client.http_retry_transport
    assert isinstance(transport, _EnvProxyHTTPTransport)
    base_kwargs = transport._transport_kwargs
    transport._make_proxied_transport = lambda proxy: fake(  # type: ignore[method-assign]
        **base_kwargs, proxy=proxy
    )
    return client


def _direct_ok(transport: httpx.AsyncHTTPTransport) -> None:
    """Serve one canned 200 from the direct pool without opening a socket."""
    transport._pool._network_backend = httpcore.AsyncMockBackend(
        [b"HTTP/1.1 200 OK\r\n", b"Content-Length: 2\r\n", b"\r\n", b"ok"]
    )


def test_default_transport_is_env_proxy_aware(proxy_env):
    client = _LoadedClient()
    assert isinstance(client.http_retry_transport, _EnvProxyHTTPTransport)
    # Still an AsyncHTTPTransport: subclass checks and pool access keep working.
    assert isinstance(client.http_retry_transport, httpx.AsyncHTTPTransport)


@pytest.mark.parametrize(
    ("env", "url", "expected"),
    [
        ({}, "https://source.example.test/x", None),
        ({"HTTPS_PROXY": _PROXY}, "https://source.example.test/x", _PROXY),
        ({"https_proxy": _PROXY}, "https://source.example.test/x", _PROXY),
        ({"HTTP_PROXY": _PROXY}, "http://source.example.test/x", _PROXY),
        ({"HTTP_PROXY": _PROXY}, "https://source.example.test/x", None),
        ({"ALL_PROXY": _PROXY}, "https://source.example.test/x", _PROXY),
        (
            {"HTTPS_PROXY": _PROXY, "NO_PROXY": "localhost,.example.test"},
            "https://source.example.test/x",
            None,
        ),
        (
            {"HTTPS_PROXY": _PROXY, "NO_PROXY": "source.example.test"},
            "https://source.example.test:8443/x",
            None,
        ),
        (
            {"HTTPS_PROXY": _PROXY, "NO_PROXY": "other.example.test"},
            "https://source.example.test/x",
            _PROXY,
        ),
        (
            {"HTTPS_PROXY": _PROXY, "NO_PROXY": "*"},
            "https://source.example.test/x",
            None,
        ),
    ],
)
def test_proxy_selection(proxy_env, env, url, expected):
    for key, value in env.items():
        proxy_env.setenv(key, value)
    transport = _EnvProxyHTTPTransport()
    assert transport._proxy_for(httpx.URL(url)) == expected


async def test_request_routes_through_proxied_transport(proxy_env, fake_proxied):
    proxy_env.setenv("HTTPS_PROXY", _PROXY)
    client = _client(fake_proxied)

    response = await client.execute_http_get_request(
        "https://source.example.test/api", raise_on_error=True
    )

    assert response is not None and response.status_code == 200
    [proxied] = fake_proxied.instances
    assert proxied.kwargs["proxy"] == _PROXY
    # The proxied transport keeps the client's TLS context, pool limits and
    # connect retries.
    assert proxied.kwargs["verify"] is not None
    assert proxied.kwargs["limits"] is not None
    assert proxied.kwargs["retries"] is not None
    assert str(proxied.requests[0].url) == "https://source.example.test/api"
    await client.close()
    assert proxied.closed


async def test_proxied_transport_is_reused(proxy_env, fake_proxied):
    proxy_env.setenv("HTTPS_PROXY", _PROXY)
    client = _client(fake_proxied)

    for _ in range(3):
        await client.execute_http_get_request(
            "https://source.example.test/api", raise_on_error=True
        )

    assert len(fake_proxied.instances) == 1
    assert len(fake_proxied.instances[0].requests) == 3
    await client.close()


async def test_no_proxy_host_goes_direct(proxy_env, fake_proxied):
    proxy_env.setenv("HTTPS_PROXY", _PROXY)
    proxy_env.setenv("NO_PROXY", ".example.test")
    client = _client(fake_proxied)
    _direct_ok(client.http_retry_transport)

    response = await client.execute_http_get_request(
        "https://source.example.test/api", raise_on_error=True
    )

    assert response is not None and response.status_code == 200
    assert fake_proxied.instances == []
    await client.close()


async def test_no_proxy_env_goes_direct(proxy_env, fake_proxied):
    client = _client(fake_proxied)
    _direct_ok(client.http_retry_transport)

    response = await client.execute_http_post_request(
        "https://source.example.test/api", json_data={}, raise_on_error=True
    )

    assert response is not None and response.status_code == 200
    assert fake_proxied.instances == []
    await client.close()


class _FailingCloseTransport(_FakeProxiedTransport):
    async def aclose(self) -> None:
        self.closed = True
        raise RuntimeError("proxied pool close failed")


@pytest.mark.parametrize("close", ["aclose", "__aexit__"])
async def test_close_failure_still_closes_every_pool(proxy_env, close):
    """One proxied pool failing to close must not leak the others or the
    direct pool; the failure is still raised."""
    transport = _EnvProxyHTTPTransport()
    failing = _FailingCloseTransport()
    healthy = _FakeProxiedTransport()
    transport._proxied = {"http://a:1": failing, "http://b:2": healthy}  # type: ignore[assignment]
    direct_closed: list[bool] = []
    original_pool_aclose = transport._pool.aclose

    async def _record_pool_close() -> None:
        direct_closed.append(True)
        await original_pool_aclose()

    transport._pool.aclose = _record_pool_close  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="proxied pool close failed"):
        if close == "aclose":
            await transport.aclose()
        else:
            await transport.__aexit__(None, None, None)

    assert failing.closed and healthy.closed
    assert direct_closed == [True]
    assert transport._proxied == {}
