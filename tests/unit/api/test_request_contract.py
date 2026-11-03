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

from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import AuthInput
from application_sdk.handler.request_contract import (
    RequestContractError,
    validate_request,
)


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
    assert caught.value.cause is not None
    assert caught.value.cause.errors()[0]["loc"] == ("timeout_seconds",)


def test_a_bare_model_validate_still_yields_the_unsafe_default() -> None:
    """Reaching the 422 takes an explicit validate_request call, so a
    ValidationError raised anywhere else keeps that route's own 500 and can
    never be reported as the caller's fault."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AuthInput.model_validate({"timeout_seconds": "soon"})


# ── a body that is not a JSON object at all ─────────────────────────────────
# The routes read the body themselves (they normalise it before validating), so
# a body that is not JSON, or is JSON but not an object, used to escape the 422
# as a bare 500. Both surfaces now answer the contract's own 422.

_NOT_AN_OBJECT = [
    b"[1]",
    b"{bad",
    b'"string"',
    # A caller-controlled body on these routes is routinely a credential.
    b"{bad fake-pw-123",
    b'["fake-pw-123"]',
    b'"fake-pw-123"',
]


def _worker() -> TestClient:
    import warnings

    from application_sdk.handler.service import create_app_handler_service

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        app = create_app_handler_service(DefaultHandler(), app_name="acme")
    return TestClient(app, raise_server_exceptions=False)


def _host() -> TestClient:
    return TestClient(
        build_asgi_app(DefaultHandler(), app_name="acme"),
        raise_server_exceptions=False,
    )


@pytest.mark.parametrize("surface", [_worker, _host], ids=["worker", "host"])
@pytest.mark.parametrize("route", ["auth", "check", "warmup", "metadata"])
@pytest.mark.parametrize("body", _NOT_AN_OBJECT, ids=lambda b: b.decode())
def test_a_body_that_is_not_a_json_object_is_422(
    surface, route: str, body: bytes
) -> None:
    resp = surface().post(
        f"/workflows/v1/{route}",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json() == {
        "success": False,
        "message": "Invalid request: body",
        "detail": [],
    }
    assert "fake-pw-123" not in resp.text
    assert "string" not in resp.text


@pytest.mark.parametrize(
    ("body", "reason"),
    [(b"{bad", "not valid JSON"), (b"[1]", "got list"), (b'"s"', "got str")],
)
async def test_read_json_object_raises_the_marker_without_a_cause(
    body: bytes, reason: str
) -> None:
    from application_sdk.handler.request_contract import read_json_object

    class _Request:
        async def json(self):
            import json

            return json.loads(body)

    with pytest.raises(RequestContractError, match=reason) as caught:
        await read_json_object(_Request())
    assert caught.value.cause is None
    assert "fake-pw-123" not in str(caught.value)


async def test_read_json_object_lets_a_non_value_error_through() -> None:
    """The body-size cap signals with a bare Exception from inside the ASGI
    receive; swallowing it would answer 422 for a body that must be a 413."""
    from application_sdk.handler.request_contract import read_json_object

    class _TooLarge(Exception):
        pass

    class _Request:
        async def json(self):
            raise _TooLarge

    with pytest.raises(_TooLarge):
        await read_json_object(_Request())
