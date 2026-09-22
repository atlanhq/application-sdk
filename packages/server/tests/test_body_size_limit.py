"""One caller must not be able to degrade every co-hosted app with a big POST.

Body reception, ``json.loads`` and the credential-dict copies all run on the
serving event loop, which on a consolidated host is shared by every hosted app
and by the kubelet probes — so an unbounded body is both a memory and a latency
problem for apps that have nothing to do with the request.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from server_sdk.handler.base import DefaultHandler
from server_sdk.server import MAX_REQUEST_BODY_BYTES, build_asgi_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(
        build_asgi_app(DefaultHandler(), app_name="acme"), raise_server_exceptions=False
    )


def test_an_over_large_declared_body_is_413(client: TestClient) -> None:
    payload = json.dumps(
        {"credentials": [{"key": "k", "value": "x" * (MAX_REQUEST_BODY_BYTES + 1000)}]}
    )
    resp = client.post(
        "/workflows/v1/auth",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413, resp.status_code
    assert resp.json()["success"] is False


def test_a_chunked_body_cannot_bypass_the_cap(client: TestClient) -> None:
    """A chunked request carries no content-length, so the up-front check alone
    would let it through — the streamed byte count is what catches it."""

    def chunks():
        chunk = b"x" * 65536
        for _ in range((MAX_REQUEST_BODY_BYTES // len(chunk)) + 4):
            yield chunk

    resp = client.post(
        "/workflows/v1/auth",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413, resp.status_code


def test_a_normal_body_is_untouched(client: TestClient) -> None:
    resp = client.post(
        "/workflows/v1/auth", json={"credentials": [{"key": "host", "value": "h"}]}
    )
    assert resp.status_code not in (413,), resp.status_code


def test_the_cap_is_generous_for_real_payloads() -> None:
    """A setup form's credentials are a few KB; the cap must not be near that."""
    assert MAX_REQUEST_BODY_BYTES >= 256 * 1024


def test_a_get_route_is_unaffected(client: TestClient) -> None:
    assert client.get("/server/health").status_code == 200


# ── the cap must never be the thing that takes the host down ────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("not-a-number", 1048576),
        ("-5", 1048576),
        ("0", 1048576),
        ("", 1048576),
        ("2048", 2048),
    ],
)
def test_an_unusable_cap_falls_back_instead_of_crashing_at_import(
    monkeypatch, value: str, expected: int
) -> None:
    """A bare int() raised ValueError at import of server_sdk.server, which
    crashloops the whole host and every app in it over one typo in a chart
    value."""
    from server_sdk.server import _max_request_body_bytes

    monkeypatch.setenv("SERVER_SDK_MAX_BODY_BYTES", value)
    assert _max_request_body_bytes() == expected


def test_the_default_applies_when_unset(monkeypatch) -> None:
    from server_sdk.server import _max_request_body_bytes

    monkeypatch.delenv("SERVER_SDK_MAX_BODY_BYTES", raising=False)
    assert _max_request_body_bytes() == 1048576
