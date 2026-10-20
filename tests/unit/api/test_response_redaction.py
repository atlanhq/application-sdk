"""No response body carries a credential, on the worker or on the host.

The log has always been redacted. The body was not: an
``AuthError("bad password for postgres://u:<pw>@h/db")`` answered 401 with the
password in ``detail``, and a handler's own success message reached the caller
verbatim. Every text field the routes build from exception or handler text now
goes through ``redact_secrets`` as the body is built — status codes, envelope
shape and every other field are unchanged.

Each case runs against both surfaces (``create_app_handler_service`` and
``build_asgi_app``), because they share one implementation and must keep doing
so.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from application_sdk.errors import AuthError
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    MetadataInput,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
)
from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.base import HandlerError
from application_sdk.handler.contracts import (
    WarmupInput,
    WarmupObservation,
    WarmupState,
)

SECRET = "fake-pw-123"
DSN = f"postgres://u:{SECRET}@h/db"
REDACTED_DSN = "postgres://***@h/db"
# Both shapes redact_secrets covers: URL userinfo and a bare credential pair.
LEAKY = f"bad password for {DSN}; password={SECRET} at login"
REDACTED = f"bad password for {REDACTED_DSN}; password=*** at login"


def _mode(credentials: list[Any]) -> str:
    return next((c.value for c in credentials if c.key == "mode"), "ok")


def _raise_for(mode: str) -> None:
    if mode == "app-error":
        raise AuthError(message=LEAKY)
    if mode == "handler-error":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            raise HandlerError(LEAKY, http_status=401)
    if mode == "crash":
        raise RuntimeError(LEAKY)


class _LeakyHandler(Handler):
    """Every route raises, or answers, with the DSN in its own text."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        _raise_for(_mode(input.credentials))
        return AuthOutput(status=AuthStatus.SUCCESS, message=LEAKY)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        _raise_for(_mode(input.credentials))
        return PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="connectivity", passed=True, message=LEAKY),
                PreflightCheck(name="tablesCheck", passed=False, message=LEAKY),
            ],
            message=LEAKY,
        )

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        _raise_for(_mode(input.credentials))
        return WarmupObservation(state=WarmupState.WARMING, source_state=LEAKY)

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        _raise_for(_mode(input.credentials))
        # Not free text: a metadata object is data and is served untouched,
        # even a value redact_secrets would mask.
        return SqlMetadataOutput(
            objects=[
                SqlMetadataObject(TABLE_CATALOG="https://h/c?owner=o", TABLE_SCHEMA="s")
            ]
        )


def _worker() -> TestClient:
    from application_sdk.handler.service import create_app_handler_service

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        app = create_app_handler_service(_LeakyHandler(), app_name="redact")
    return TestClient(app, raise_server_exceptions=False)


def _host() -> TestClient:
    return TestClient(
        build_asgi_app(_LeakyHandler(), app_name="redact"),
        raise_server_exceptions=False,
    )


SURFACES: dict[str, Callable[[], TestClient]] = {"worker": _worker, "host": _host}


@pytest.fixture(params=sorted(SURFACES))
def client(request: pytest.FixtureRequest) -> TestClient:
    return SURFACES[request.param]()


def _post(client: TestClient, route: str, mode: str) -> Any:
    resp = client.post(
        f"/workflows/v1/{route}",
        json={
            "credentials": [{"key": "mode", "value": mode}],
            "connection_config": {"host": "h"},
        },
    )
    assert SECRET not in resp.text, resp.text
    return resp


# ── error paths ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("route", ["auth", "warmup", "metadata"])
def test_an_app_error_detail_is_redacted(client: TestClient, route: str) -> None:
    resp = _post(client, route, "app-error")
    assert resp.status_code == 401
    assert resp.json() == {"detail": REDACTED}


@pytest.mark.parametrize("route", ["auth", "metadata"])
def test_a_handler_error_detail_is_redacted(client: TestClient, route: str) -> None:
    """The deprecated path keeps its ``[CODE]`` prefix and suffixes."""
    resp = _post(client, route, "handler-error")
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert REDACTED in detail
    assert detail.startswith("[")


@pytest.mark.parametrize("route", ["auth", "warmup", "metadata"])
def test_an_unexpected_exception_is_a_fixed_500(client: TestClient, route: str) -> None:
    resp = _post(client, route, "crash")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal server error"}


@pytest.mark.parametrize(
    ("mode", "status"), [("app-error", 401), ("handler-error", 401), ("crash", 500)]
)
def test_a_check_raise_carries_no_secret(
    client: TestClient, mode: str, status: int
) -> None:
    resp = _post(client, "check", mode)
    assert resp.status_code == status
    body = resp.json()
    assert body["success"] is False
    assert body["preflight"]["status"] == "not_ready"
    assert {"data", "preflight", "detail", "error"} <= body.keys()
    if mode == "crash":
        assert body["detail"] == "Internal server error"
    else:
        assert body["detail"] == REDACTED


# ── success bodies whose message carries the DSN ────────────────────────────


def test_auth_success_messages_are_redacted(client: TestClient) -> None:
    resp = _post(client, "auth", "ok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["message"] == REDACTED
    assert body["data"]["message"] == REDACTED
    assert body["data"]["status"] == "success"


def test_check_success_messages_are_redacted(client: TestClient) -> None:
    resp = _post(client, "check", "ok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["message"] == REDACTED
    assert body["data"]["connectivity"] == {
        "success": True,
        "message": REDACTED,
        "successMessage": REDACTED,
        "failureMessage": "",
    }
    assert body["data"]["tablesCheck"]["failureMessage"] == REDACTED
    preflight = body["preflight"]
    assert preflight["status"] == "not_ready"
    assert preflight["message"] == REDACTED
    assert [c["message"] for c in preflight["checks"]] == [REDACTED, REDACTED]


def test_warmup_source_state_is_redacted(client: TestClient) -> None:
    resp = _post(client, "warmup", "ok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Warmup warming"
    assert body["data"]["state"] == "warming"
    assert body["data"]["source_state"] == REDACTED


def test_metadata_data_is_not_redacted(client: TestClient) -> None:
    """Redaction is for free text only: a metadata body has no message (the
    filter widgets break on one), and its objects are served as they are."""
    resp = _post(client, "metadata", "ok")
    assert resp.status_code == 200
    body = resp.json()
    assert "message" not in body
    assert body["data"][0]["TABLE_CATALOG"] == "https://h/c?owner=o"


# ── the manifest's fe_inputs ────────────────────────────────────────────────


def test_malformed_fe_inputs_answers_a_fixed_message() -> None:
    from fastapi import HTTPException

    from application_sdk.handler.metadata_routes import decode_fe_inputs

    with pytest.raises(HTTPException) as caught:
        decode_fe_inputs(f"{{password={SECRET}")
    assert caught.value.status_code == 400
    assert caught.value.detail == "fe_inputs is not valid JSON"


def test_malformed_manifest_body_answers_a_fixed_message() -> None:
    """The parser's text never reaches the caller: a fixed 400, as fe_inputs.

    Worker only: the host registers the metadata routes only for an app that
    ships ``generated/``, and both surfaces share ``register_metadata_routes``.
    """
    resp = _worker().post(
        "/workflows/v1/manifest",
        content=f'{{"fe_inputs": {{"password": "{SECRET}"'.encode(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Request body is not valid JSON"
    assert SECRET not in resp.text
