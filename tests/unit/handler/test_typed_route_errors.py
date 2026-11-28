"""A typed error raised in a handler route reaches the caller as a typed error.

The route boundary used to answer an ``AppError`` with its status and a bare
``{"detail": message}``: category, code, audience, retryable and the suggested
action were dropped, and a caller that reads ``message`` (as the platform's
workflow-save path does) showed the raw body instead of the app's message.
The boundary now answers ``{"detail", "message", "error"}`` -- ``detail`` kept
for older callers, ``error`` the redacted ``FailureDetails`` the preflight route
already sends. An untyped exception still answers a fixed 500 with nothing else.

Every case runs on both surfaces: the worker's handler service and the API host.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from application_sdk.errors import AuthError
from application_sdk.errors.leaves import DependencyUnavailableError, InvalidInputError
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    ManifestInput,
    MetadataInput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)
from application_sdk.handler.asgi import build_asgi_app

SECRET = "fake-pw-123"
DSN = f"postgres://u:{SECRET}@h/db"
USER_MESSAGE = "Propagation direction 'x' is not supported."
USER_ACTION = "Choose Lineage, Hierarchy or Relationship."
OUTAGE_MESSAGE = "The Atlan API is not available."

_GENERATED = {
    "execution_mode": "automation-engine",
    "dag": {
        "run": {
            "activity_name": "execute_workflow",
            "inputs": {"task_queue": "atlan-typed-{deployment_name}", "args": {}},
        }
    },
}


def _raise_for(mode: str) -> None:
    if mode == "user":
        raise InvalidInputError(
            message=USER_MESSAGE, suggested_action=USER_ACTION, field="direction"
        )
    if mode == "outage":
        raise DependencyUnavailableError(message=OUTAGE_MESSAGE)
    if mode == "auth-evidence":
        raise AuthError(message="Authentication failed", failure_reason=DSN)
    if mode == "crash":
        raise ValueError(f"boom for {DSN}")


class _RaisingHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        _raise_for(next(c.value for c in input.credentials if c.key == "mode"))
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        _raise_for(next(c.value for c in input.credentials if c.key == "mode"))
        return SqlMetadataOutput(objects=[])

    async def compute_manifest(self, input: ManifestInput) -> dict[str, Any] | None:
        _raise_for(str(input.fe_inputs.get("mode", "")))
        return None


@pytest.fixture
def generated(tmp_path: Path) -> Path:
    gen = tmp_path / "generated" / "run"
    gen.mkdir(parents=True)
    (gen / "manifest.json").write_text(json.dumps(_GENERATED))
    return gen.parent


@pytest.fixture(params=["worker", "host"])
def client(
    request: pytest.FixtureRequest, generated: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    import application_sdk.handler.service as svc_module
    from application_sdk.handler.service import create_app_handler_service

    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "typed")
    monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "prod")
    monkeypatch.setenv("ATLAN_APPENV__TYPED__ATLAN_DEPLOYMENT_NAME", "prod")
    monkeypatch.setattr(svc_module, "CONTRACT_GENERATED_DIR", generated)
    if request.param == "worker":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            app = create_app_handler_service(_RaisingHandler(), app_name="typed")
    else:
        app = build_asgi_app(
            _RaisingHandler(), app_name="typed", generated_dir=generated
        )
    return TestClient(app, raise_server_exceptions=False)


def _manifest(client: TestClient, mode: str) -> Any:
    return client.post(
        "/manifest", json={"entrypoint": "run", "fe_inputs": {"mode": mode}}
    )


def _credential_route(route: str) -> Callable[[TestClient, str], Any]:
    def call(client: TestClient, mode: str) -> Any:
        return client.post(
            f"/workflows/v1/{route}",
            json={
                "credentials": [{"key": "mode", "value": mode}],
                "connection_config": {"host": "h"},
            },
        )

    return call


ROUTES: dict[str, Callable[[TestClient, str], Any]] = {
    "manifest": _manifest,
    "auth": _credential_route("auth"),
    "metadata": _credential_route("metadata"),
}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_user_error_carries_its_message_and_suggested_action(
    client: TestClient, route: str
) -> None:
    resp = ROUTES[route](client, "user")
    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"] == USER_MESSAGE
    assert body["message"] == USER_MESSAGE
    error = body["error"]
    assert error["category"] == "INVALID_INPUT"
    assert error["code"] == InvalidInputError.code
    assert error["audience"] == InvalidInputError.audience.value
    assert error["message"] == USER_MESSAGE
    assert error["suggested_action"] == USER_ACTION
    assert error["evidence"]["field"] == "direction"
    assert "cause_repr" not in error


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_an_outage_is_reported_as_an_outage(client: TestClient, route: str) -> None:
    resp = ROUTES[route](client, "outage")
    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"] == body["message"] == OUTAGE_MESSAGE
    assert body["error"]["category"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["retryable"] is True
    # Only a typed error gets the typed body: an untyped one is still a bare
    # 500 whose text stays in the log.
    crash = ROUTES[route](client, "crash")
    assert crash.status_code == 500
    assert crash.json() == {"detail": "Internal server error"}


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_typed_error_evidence_is_redacted(client: TestClient, route: str) -> None:
    resp = ROUTES[route](client, "auth-evidence")
    assert resp.status_code == 401
    assert SECRET not in resp.text, resp.text
    assert resp.json()["error"]["evidence"]["failure_reason"] == "postgres://***@h/db"
