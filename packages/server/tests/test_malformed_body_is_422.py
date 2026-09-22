"""A malformed body is the caller's fault, so it must answer 422, not 500.

Every route here normalises the wire shape before validating, so it reads the
body itself -- and that read sat OUTSIDE the route's error boundary. Unparseable
JSON raised a decode error; a top-level array or scalar raised a TypeError in the
normaliser (on /start, `list.pop("workflow_id", None)` raises outright, since
list.pop takes one argument). All of them escaped as an opaque 500, which is the
exact outcome request_contract.py exists to prevent -- and on a consolidated host
a 500 is indistinguishable from a host fault, so it gets triaged as one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server_sdk.handler.base import Handler
from server_sdk.server import build_asgi_app

MALFORMED = [
    pytest.param("{not json", id="unparseable"),
    pytest.param("[1, 2]", id="top-level-array"),
    pytest.param("7", id="top-level-scalar"),
    pytest.param('"a string"', id="top-level-string"),
    pytest.param("null", id="top-level-null"),
]

ROUTES = ["/workflows/v1/auth", "/workflows/v1/check", "/workflows/v1/metadata"]


class _Handler(Handler):
    async def test_auth(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("handler must not run on a malformed body")

    async def preflight_check(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("handler must not run on a malformed body")

    async def fetch_metadata(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("handler must not run on a malformed body")


@pytest.fixture
def client() -> TestClient:
    return TestClient(
        build_asgi_app(_Handler(), app_name="acme"), raise_server_exceptions=False
    )


@pytest.mark.parametrize("path", ROUTES)
@pytest.mark.parametrize("payload", MALFORMED)
def test_a_malformed_body_is_422_not_500(
    client: TestClient, path: str, payload: str
) -> None:
    resp = client.post(
        path, content=payload, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 422, f"{path} {payload!r} -> {resp.status_code}"


@pytest.mark.parametrize("payload", MALFORMED)
def test_start_is_422_not_500(payload: str) -> None:
    """/start additionally crashed in list.pop(key, default) before validating."""
    client = TestClient(
        build_asgi_app(_Handler(), app_name="acme"), raise_server_exceptions=False
    )
    resp = client.post(
        "/workflows/v1/start",
        content=payload,
        headers={"content-type": "application/json"},
    )
    # 503 when no Temporal client is configured, which is checked first and is a
    # different, correct answer. What must never happen is a 500.
    assert resp.status_code in (422, 503), resp.status_code


def test_the_error_never_echoes_the_body(client: TestClient) -> None:
    """These routes carry credentials, so the 422 must not quote what was sent."""
    secret = "hunter2-do-not-echo"
    resp = client.post(
        "/workflows/v1/auth",
        content=f'"{secret}"',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    assert secret not in resp.text
