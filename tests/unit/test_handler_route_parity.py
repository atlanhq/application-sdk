"""The worker's handler service and the API host answer identically.

Both register ``application_sdk.routes.register_handler_routes``, so for the
same handler and the same request the status code and body must be byte-for-byte
equal. This is the guarantee that lets one app handler serve both surfaces: a
verdict can never differ between the setup UI (host) and the app's own server.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from application_sdk.errors import AuthError, SourceUnavailableError
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


@dataclass(kw_only=True)
class _SourceRestarting(SourceUnavailableError):
    code: ClassVar[str] = "SOURCE_UNAVAILABLE_PARITY_RESTARTING"


class _ParityHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        user = next((c.value for c in input.credentials if c.key == "username"), "")
        if user == "bad":
            raise AuthError(message="denied for warehouse://svc:hunter2@db.internal/x")
        if user == "down":
            raise _SourceRestarting(message="source restarting")
        if user == "boom":
            raise RuntimeError("driver exploded")
        return AuthOutput(status=AuthStatus.SUCCESS, message="ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="connectivity", passed=True, message="reachable"),
                PreflightCheck(name="tablesCheck", passed=False, message="no grant"),
            ],
            message="1 check failed",
        )

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(
            objects=[SqlMetadataObject(TABLE_CATALOG="c", TABLE_SCHEMA="s")]
        )


def _worker_client() -> TestClient:
    from application_sdk.handler.service import create_app_handler_service

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        app = create_app_handler_service(_ParityHandler(), app_name="parity")
    return TestClient(app, raise_server_exceptions=False)


def _host_client() -> TestClient:
    return TestClient(
        build_asgi_app(_ParityHandler(), app_name="parity"),
        raise_server_exceptions=False,
    )


def _creds(user: str) -> dict[str, Any]:
    return {
        "credentials": [
            {"key": "username", "value": user},
            {"key": "password", "value": "p"},
        ]
    }


_CASES: list[tuple[str, str, Any]] = [
    ("auth-success", "/workflows/v1/auth", _creds("ok")),
    ("auth-typed-error-401", "/workflows/v1/auth", _creds("bad")),
    ("auth-source-down-503", "/workflows/v1/auth", _creds("down")),
    ("auth-crash-500", "/workflows/v1/auth", _creds("boom")),
    (
        "auth-v2-flat-credentials",
        "/workflows/v1/auth",
        {"username": "ok", "password": "p"},
    ),
    (
        "check-verdict",
        "/workflows/v1/check",
        {**_creds("ok"), "connectionConfig": {"a": 1}},
    ),
    ("metadata", "/workflows/v1/metadata", _creds("ok")),
    ("malformed-field-422", "/workflows/v1/auth", {"credentials": "not-a-list"}),
]


def _normalise(body: Any) -> Any:
    """Drop the per-request id, the only field that is legitimately different."""
    if isinstance(body, dict):
        return {k: _normalise(v) for k, v in body.items() if k not in {"request_id"}}
    if isinstance(body, list):
        return [_normalise(v) for v in body]
    return body


@pytest.mark.parametrize(
    ("case", "path", "payload"), _CASES, ids=[c[0] for c in _CASES]
)
def test_worker_and_host_answer_identically(case: str, path: str, payload: Any) -> None:
    worker = _worker_client().post(path, json=payload)
    host = _host_client().post(path, json=payload)
    assert worker.status_code == host.status_code, (case, worker.text, host.text)
    assert _normalise(worker.json()) == _normalise(host.json()), case


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"scalar"'])
def test_a_malformed_body_is_422_on_both(raw: bytes) -> None:
    for client in (_worker_client(), _host_client()):
        resp = client.post(
            "/workflows/v1/auth",
            content=raw,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422, resp.text


def test_no_secret_reaches_either_body() -> None:
    for client in (_worker_client(), _host_client()):
        resp = client.post("/workflows/v1/auth", json=_creds("bad"))
        assert resp.status_code == 401
        assert "hunter2" not in resp.text


# ── app-defined routes (Handler.routers) ─────────────────────────────────────


def _projects_router():
    from fastapi import APIRouter

    router = APIRouter(prefix="/workflows/v1/metadata")

    @router.post("/projects")
    async def projects(body: dict[str, Any]) -> dict[str, Any]:
        return {"projects": ["p1", "p2"], "echo": body.get("q")}

    return router


class _RoutedHandler(_ParityHandler):
    def routers(self):
        return [_projects_router()]


def test_an_app_router_is_served_identically_by_worker_and_host() -> None:
    from application_sdk.handler.service import create_app_handler_service

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        worker = TestClient(
            create_app_handler_service(_RoutedHandler(), app_name="parity"),
            raise_server_exceptions=False,
        )
    host = TestClient(
        build_asgi_app(_RoutedHandler(), app_name="parity"),
        raise_server_exceptions=False,
    )
    responses = [
        c.post("/workflows/v1/metadata/projects", json={"q": "x"})
        for c in (worker, host)
    ]
    assert [r.status_code for r in responses] == [200, 200]
    assert (
        responses[0].json()
        == responses[1].json()
        == {
            "projects": ["p1", "p2"],
            "echo": "x",
        }
    )


def test_a_router_that_redefines_an_sdk_route_is_refused() -> None:
    from fastapi import APIRouter

    router = APIRouter()

    @router.post("/workflows/v1/auth")
    async def hijack() -> dict[str, str]:
        return {}

    class _Hijack(_ParityHandler):
        def routers(self):
            return [router]

    with pytest.raises(ValueError, match="redefines POST /workflows/v1/auth"):
        build_asgi_app(_Hijack(), app_name="parity")


def test_a_handler_without_routers_adds_nothing() -> None:
    paths = {
        getattr(r, "path", "")
        for r in build_asgi_app(_ParityHandler(), app_name="p").routes
    }
    assert "/workflows/v1/metadata/projects" not in paths


def test_the_worker_refuses_a_router_that_redefines_a_workflow_route() -> None:
    """The worker registers more SDK routes after the handler routes; the check sees them."""
    from fastapi import APIRouter

    from application_sdk.handler.service import create_app_handler_service

    router = APIRouter()

    @router.post("/workflows/v1/start")
    async def start() -> dict[str, str]:
        return {}

    class _Hijack(_ParityHandler):
        def routers(self):
            return [router]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ValueError, match="redefines POST /workflows/v1/start"):
            create_app_handler_service(_Hijack(), app_name="parity")
