"""Validation tests for `application_sdk.routing.host_apps`.

Lifted from the PoC at /tmp/consolidation-poc/. Each test asserts a specific
behavioural claim that the consolidation design depends on. Failures here
mean the design has broken.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from application_sdk.routing import (
    DEFAULT_SHARED_NAMESPACE,
    current_app_name,
    host_apps,
)


def _make_app(label: str) -> FastAPI:
    """Build a minimal sub-app exposing the routes a real app would have."""
    sub = FastAPI()

    @sub.get("/health")
    async def health() -> dict:
        return {"status": "ok", "app": label}

    @sub.post("/workflows/v1/start")
    async def start(payload: dict) -> dict:
        return {"app": label, "echoed": payload, "current": current_app_name.get()}

    @sub.post("/workflows/v1/auth")
    async def auth(payload: dict) -> dict:
        return {"app": label, "auth_ok": True}

    return sub


@pytest.fixture
def client() -> httpx.AsyncClient:
    parent = host_apps(
        [
            ("alpha", _make_app("alpha")),
            ("beta", _make_app("beta")),
        ]
    )
    return httpx.AsyncClient(transport=ASGITransport(app=parent), base_url="http://t")


def _alpha_host() -> str:
    return f"alpha.{DEFAULT_SHARED_NAMESPACE}.svc.cluster.local"


def _beta_host() -> str:
    return f"beta.{DEFAULT_SHARED_NAMESPACE}.svc.cluster.local"


@pytest.mark.asyncio
async def test_host_dispatches_to_correct_app(client: httpx.AsyncClient) -> None:
    """alpha Host → alpha; beta Host → beta. Same path, no collision."""
    async with client:
        ra = await client.post(
            "/workflows/v1/start", headers={"Host": _alpha_host()}, json={"k": "a"}
        )
        rb = await client.post(
            "/workflows/v1/start", headers={"Host": _beta_host()}, json={"k": "b"}
        )

    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text
    assert ra.json()["app"] == "alpha"
    assert rb.json()["app"] == "beta"
    assert ra.json()["echoed"] == {"k": "a"}
    assert rb.json()["echoed"] == {"k": "b"}


@pytest.mark.asyncio
async def test_per_app_health_under_host_dispatch(client: httpx.AsyncClient) -> None:
    """Per-app `/health` is reachable via the app's Host. The parent `/health`
    must be registered AFTER Host routes; otherwise it shadows."""
    async with client:
        r = await client.get("/health", headers={"Host": _alpha_host()})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "app": "alpha"}


@pytest.mark.asyncio
async def test_pod_level_health_for_unknown_host() -> None:
    """When the Host doesn't match any app (kubelet probe with pod IP),
    fall through to the parent `/health` answering `scope=pod`."""
    parent = host_apps([("alpha", _make_app("alpha"))])
    async with httpx.AsyncClient(
        transport=ASGITransport(app=parent), base_url="http://t"
    ) as c:
        r = await c.get("/health", headers={"Host": "10.0.0.1"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "scope": "pod"}


@pytest.mark.asyncio
async def test_unknown_host_returns_404(client: httpx.AsyncClient) -> None:
    """A Host that matches no app must NOT silently dispatch — 404."""
    async with client:
        r = await client.post(
            "/workflows/v1/start",
            headers={"Host": "ghost.common-app-server.svc.cluster.local"},
            json={},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_port_suffix_tolerated(client: httpx.AsyncClient) -> None:
    """K8s Service-to-pod traffic may include the port; both `:80` and an
    arbitrary port should match the same app."""
    async with client:
        r80 = await client.post(
            "/workflows/v1/start",
            headers={"Host": f"{_alpha_host()}:80"},
            json={},
        )
        r8000 = await client.post(
            "/workflows/v1/start",
            headers={"Host": f"{_alpha_host()}:8000"},
            json={},
        )
    assert r80.status_code == 200
    assert r80.json()["app"] == "alpha"
    assert r8000.status_code == 200
    assert r8000.json()["app"] == "alpha"


@pytest.mark.asyncio
async def test_host_match_is_case_insensitive(client: httpx.AsyncClient) -> None:
    """RFC 7230 §5.4 — Host headers are case-insensitive. Mixed-case Hosts
    must still match; the LowercaseHostMiddleware is what makes this true."""
    async with client:
        r = await client.post(
            "/workflows/v1/start",
            headers={"Host": _alpha_host().upper()},
            json={},
        )
    assert r.status_code == 200
    assert r.json()["app"] == "alpha"


@pytest.mark.asyncio
async def test_short_forms_match() -> None:
    """In-cluster clients may use short forms — bare `alpha` (same namespace)
    or `alpha.{namespace}` (cluster-DNS short form). Both must dispatch."""
    parent = host_apps([("alpha", _make_app("alpha"))])
    async with httpx.AsyncClient(
        transport=ASGITransport(app=parent), base_url="http://t"
    ) as c:
        r1 = await c.get("/health", headers={"Host": "alpha"})
        r2 = await c.get(
            "/health", headers={"Host": f"alpha.{DEFAULT_SHARED_NAMESPACE}"}
        )
    assert r1.status_code == 200 and r1.json()["app"] == "alpha"
    assert r2.status_code == 200 and r2.json()["app"] == "alpha"


@pytest.mark.asyncio
async def test_current_app_name_contextvar_set(client: httpx.AsyncClient) -> None:
    """Code inside a request handler can read `current_app_name` to learn
    which app context it's running in — replaces the process-level
    `application_sdk.constants.APPLICATION_NAME` for multi-app processes."""
    async with client:
        ra = await client.post(
            "/workflows/v1/start", headers={"Host": _alpha_host()}, json={}
        )
        rb = await client.post(
            "/workflows/v1/start", headers={"Host": _beta_host()}, json={}
        )
    assert ra.json()["current"] == "alpha"
    assert rb.json()["current"] == "beta"


@pytest.mark.asyncio
async def test_duplicate_app_name_rejected() -> None:
    """Configuration error caught at startup, not at request time."""
    with pytest.raises(ValueError, match="duplicate"):
        host_apps([("alpha", _make_app("a1")), ("alpha", _make_app("a2"))])


@pytest.mark.asyncio
async def test_concurrent_dispatch_no_crosstalk() -> None:
    """500 concurrent interleaved requests across two apps must each land at
    the correct sub-app (catches event-loop races and ContextVar leakage)."""
    parent = host_apps([("alpha", _make_app("alpha")), ("beta", _make_app("beta"))])
    async with httpx.AsyncClient(
        transport=ASGITransport(app=parent), base_url="http://t"
    ) as c:

        async def fire(idx: int) -> tuple[str, dict]:
            host = _alpha_host() if idx % 2 == 0 else _beta_host()
            expected = "alpha" if idx % 2 == 0 else "beta"
            r = await c.post(
                "/workflows/v1/start",
                headers={"Host": host},
                json={"idx": idx},
            )
            return expected, r.json()

        results = await asyncio.gather(*(fire(i) for i in range(500)))

    for expected, body in results:
        assert body["app"] == expected, body
        assert body["current"] == expected, body


@pytest.mark.asyncio
async def test_namespace_override_via_argument() -> None:
    """`shared_namespace` argument overrides the default."""
    parent = host_apps([("alpha", _make_app("alpha"))], shared_namespace="custom-ns")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=parent), base_url="http://t"
    ) as c:
        r_match = await c.get(
            "/health", headers={"Host": "alpha.custom-ns.svc.cluster.local"}
        )
        r_default = await c.get("/health", headers={"Host": _alpha_host()})
    assert r_match.status_code == 200 and r_match.json()["app"] == "alpha"
    # Default pattern should NOT match when override is set
    assert r_default.status_code == 200
    assert r_default.json().get("scope") == "pod"


@pytest.mark.asyncio
async def test_namespace_override_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ATLAN_COMMON_APP_NAMESPACE` env var overrides the default."""
    monkeypatch.setenv("ATLAN_COMMON_APP_NAMESPACE", "env-ns")
    parent = host_apps([("alpha", _make_app("alpha"))])
    async with httpx.AsyncClient(
        transport=ASGITransport(app=parent), base_url="http://t"
    ) as c:
        r = await c.get("/health", headers={"Host": "alpha.env-ns.svc.cluster.local"})
    assert r.status_code == 200 and r.json()["app"] == "alpha"
