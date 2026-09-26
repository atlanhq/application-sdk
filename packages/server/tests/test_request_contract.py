"""A malformed request body is the caller's fault: 422, not 500.

All three handler routes normalise the body themselves (they accept v2 and v3
credential shapes), so FastAPI's own RequestValidationError path never sees it.
A bare ``model_validate`` raised pydantic's ValidationError inside the route,
which escapes to ServerErrorMiddleware as an opaque 500 -- and on a
consolidated host a 500 reads as a host fault and gets triaged as one.

The realistic trigger is a v3 credentials list carrying a non-string value: the
two accepted wire shapes disagree, because the v2 path stringifies and the v3
path hands the raw list to HandlerCredential, whose key/value are ``str``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server_sdk.handler.base import DefaultHandler
from server_sdk.handler.contracts import AuthInput
from server_sdk.handler.request_contract import RequestContractError, validate_request
from server_sdk.server import build_asgi_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(
        build_asgi_app(DefaultHandler(), app_name="acme"),
        raise_server_exceptions=False,
    )


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        # The one that actually reaches a tenant: a form submitting a numeric
        # port under the v3 list shape.
        (
            "/workflows/v1/auth",
            {"credentials": [{"key": "port", "value": 5439}]},
            "credentials.0.value",
        ),
        ("/workflows/v1/auth", {"timeout_seconds": "soon"}, "timeout_seconds"),
        ("/workflows/v1/check", {"checks_to_run": "all"}, "checks_to_run"),
        ("/workflows/v1/metadata", {"max_objects": "lots"}, "max_objects"),
    ],
)
def test_malformed_body_is_422_naming_the_field(
    client: TestClient, path: str, body: dict, field: str
) -> None:
    resp = client.post(path, json=body)
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["success"] is False
    assert field in payload["message"]
    assert any(item["field"] == field for item in payload["detail"])


def test_the_rejected_value_is_never_echoed(client: TestClient) -> None:
    """The offending value can be a credential; the field path is the diagnosis."""
    resp = client.post(
        "/workflows/v1/auth",
        json={"credentials": [{"key": "password", "value": {"nested": "hunter2"}}]},
    )
    assert resp.status_code == 422
    assert "hunter2" not in resp.text


def test_the_v2_shape_still_passes(client: TestClient) -> None:
    """v2 stringifies, so a numeric port is legal there — the asymmetry that
    made the v3 case a surprise."""
    resp = client.post(
        "/workflows/v1/auth", json={"credentials": {"host": "h", "port": 5439}}
    )
    assert resp.status_code != 422


def test_a_valid_body_is_untouched(client: TestClient) -> None:
    resp = client.post(
        "/workflows/v1/auth",
        json={"credentials": [{"key": "host", "value": "h"}]},
    )
    assert resp.status_code != 422


# ── the marker is the enforcement ───────────────────────────────────────────


def test_validate_request_raises_the_marker() -> None:
    with pytest.raises(RequestContractError) as caught:
        validate_request(AuthInput, {"timeout_seconds": "soon"})
    assert caught.value.cause.errors()[0]["loc"] == ("timeout_seconds",)


def test_a_bare_model_validate_still_yields_the_unsafe_default() -> None:
    """Reaching the 422 takes an explicit validate_request call, so a
    ValidationError raised anywhere else keeps that route's own 500 and can
    never be reported as the caller's fault."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AuthInput.model_validate({"timeout_seconds": "soon"})
