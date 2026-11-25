"""One proxy rule for every SDK connection (:mod:`application_sdk._proxy`).

The proxy for a scheme is that scheme's variable, then ``ALL_PROXY``; a
``NO_PROXY`` match connects directly; only the process environment is read.
The agreement test drives the real call sites: before this rule existed,
``BaseClient``, obstore and Temporal each resolved the same environment
differently, so on a proxy-only host some connections worked and others timed
out.
"""

from __future__ import annotations

import urllib.request
from typing import Any

import httpx
import pytest

from application_sdk._obstore_config import obstore_client_options
from application_sdk._proxy import environment_proxies, no_proxy, proxy_for
from application_sdk.clients.base import _EnvProxyHTTPTransport
from application_sdk.execution._temporal.backend import _build_temporal_proxy_config

_PROXY = "http://proxy.internal:3128"
_HOST = "source.example.test"


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A monkeypatch with every proxy variable unset, in either spelling."""
    for scheme in ("https", "http", "all", "no"):
        monkeypatch.delenv(f"{scheme}_proxy", raising=False)
        monkeypatch.delenv(f"{scheme.upper()}_PROXY", raising=False)
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    return monkeypatch


def _set(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _base_client(host: str) -> str | None:
    return _EnvProxyHTTPTransport()._proxy_for(httpx.URL(f"https://{host}/x"))


def _obstore(host: str) -> str | None:
    """What obstore does with the options it is given.

    obstore receives one ``proxy_url`` and applies ``proxy_excludes`` per
    request itself, so the effective answer for *host* is the proxy unless
    the host is in the excludes it was handed.
    """
    options: dict[str, Any] = dict(obstore_client_options())
    proxy = options.get("proxy_url")
    if proxy is None:
        return None
    excludes = options.get("proxy_excludes")
    if excludes and urllib.request.proxy_bypass_environment(  # pyright: ignore[reportAttributeAccessIssue]
        host, {"no": excludes}
    ):
        return None
    return proxy


def _temporal(host: str) -> str | None:
    config = _build_temporal_proxy_config(f"{host}:7233", tls_enabled=True)
    return None if config is None else f"http://{config.target_host}"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        pytest.param({}, None, id="unset"),
        pytest.param({"HTTPS_PROXY": _PROXY}, _PROXY, id="https-proxy-only"),
        pytest.param({"HTTP_PROXY": _PROXY}, None, id="http-proxy-only"),
        pytest.param({"ALL_PROXY": _PROXY}, _PROXY, id="all-proxy-only"),
        pytest.param(
            {"HTTPS_PROXY": _PROXY, "NO_PROXY": ".example.test"},
            None,
            id="no-proxy-match",
        ),
        pytest.param(
            {"ALL_PROXY": _PROXY, "NO_PROXY": "other.test"},
            _PROXY,
            id="no-proxy-miss",
        ),
    ],
)
def test_call_sites_agree(proxy_env, env, expected):
    """BaseClient, obstore and Temporal resolve the same TLS connection alike."""
    _set(proxy_env, env)
    assert {
        "base_client": _base_client(_HOST),
        "obstore": _obstore(_HOST),
        "temporal": _temporal(_HOST),
    } == dict.fromkeys(("base_client", "obstore", "temporal"), expected)


class TestProxyFor:
    def test_scheme_variable_wins_over_all_proxy(self, proxy_env):
        _set(proxy_env, {"HTTPS_PROXY": _PROXY, "ALL_PROXY": "http://all:1"})
        assert proxy_for("https") == _PROXY

    def test_no_https_to_http_fallback(self, proxy_env):
        _set(proxy_env, {"HTTP_PROXY": _PROXY})
        assert proxy_for("https") is None
        assert proxy_for("http") == _PROXY

    def test_scheme_is_case_insensitive(self, proxy_env):
        _set(proxy_env, {"HTTPS_PROXY": _PROXY})
        assert proxy_for("HTTPS") == _PROXY

    def test_lowercase_variable_is_read(self, proxy_env):
        _set(proxy_env, {"https_proxy": _PROXY})
        assert proxy_for("https") == _PROXY

    @pytest.mark.parametrize(
        ("no_proxy_value", "host", "bypassed"),
        [
            ("source.example.test", "source.example.test", True),
            ("source.example.test", "source.example.test:8443", True),
            (".example.test", "source.example.test", True),
            ("example.test", "source.example.test", True),
            ("*", "anything.test", True),
            ("other.test", "source.example.test", False),
        ],
    )
    def test_no_proxy_matching(self, proxy_env, no_proxy_value, host, bypassed):
        _set(proxy_env, {"HTTPS_PROXY": _PROXY, "NO_PROXY": no_proxy_value})
        assert proxy_for("https", host) == (None if bypassed else _PROXY)

    def test_no_host_skips_no_proxy(self, proxy_env):
        _set(proxy_env, {"HTTPS_PROXY": _PROXY, "NO_PROXY": "*"})
        assert proxy_for("https") == _PROXY

    def test_snapshot_is_used_instead_of_environment(self, proxy_env):
        _set(proxy_env, {"HTTPS_PROXY": _PROXY})
        snapshot = environment_proxies()
        proxy_env.delenv("HTTPS_PROXY")
        assert proxy_for("https", _HOST, proxies=snapshot) == _PROXY
        assert proxy_for("https", _HOST) is None


class TestNoProxy:
    def test_returns_raw_list(self, proxy_env):
        _set(proxy_env, {"NO_PROXY": "localhost,.svc.cluster.local"})
        assert no_proxy() == "localhost,.svc.cluster.local"

    @pytest.mark.parametrize("value", [None, ""])
    def test_unset_or_empty_is_none(self, proxy_env, value):
        if value is not None:
            _set(proxy_env, {"NO_PROXY": value})
        assert no_proxy() is None
