"""Handler service tests: /health, /server/ready and the auth endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from application_sdk.handler.contracts import (
    AuthStatus,
)
from application_sdk.handler.service import (
    create_app_handler_service,
)
from application_sdk.server.health import WorkerHealthServer
from tests.unit.handler._service_support import (
    _AuthCausedFailureHandler,
    _AuthExpiredHandler,
    _AuthFailedHandler,
    _AuthInvalidCredsHandler,
    _AuthTypedFailureHandler,
    _AuthUnhandledExceptionHandler,
    _FailingHandler,
    _make_client,
    _SourceDownHandler,
    _TestHandler,
)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthEndpoints:
    """Tests for /health and /server/ready."""

    def test_health_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_server_health_alias_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_ready_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_ready_alias_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestReadyDelegatesToWorkerHealth:
    """Combined mode: the handler's /ready mirrors the worker's (FND-4644)."""

    @staticmethod
    def _client(worker_health: WorkerHealthServer) -> TestClient:
        app = create_app_handler_service(
            _TestHandler(), app_name="test-app", worker_health=worker_health
        )
        return TestClient(app)

    @pytest.mark.parametrize("path", ["/ready", "/server/ready"])
    def test_not_ready_worker_returns_503(self, path: str) -> None:
        # No Temporal client configured: the worker port's /ready is 503.
        client = self._client(WorkerHealthServer(port=0))
        response = client.get(path)
        assert response.status_code == 503
        body = response.json()
        assert body["healthy"] is False
        assert body["message"] == "Temporal client not configured"

    @pytest.mark.parametrize("path", ["/ready", "/server/ready"])
    def test_ready_worker_returns_worker_body(self, path: str) -> None:
        class _Client:
            identity = "worker-1"

        worker_health = WorkerHealthServer(port=0)
        worker_health.set_temporal_client(_Client())
        client = self._client(worker_health)
        response = client.get(path)
        assert response.status_code == 200
        body = response.json()
        assert body["healthy"] is True
        assert body["details"]["identity"] == "worker-1"

    def test_health_stays_static(self) -> None:
        # /health is a process-up probe on both ports; only /ready delegates.
        client = self._client(WorkerHealthServer(port=0))
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}


class TestAuthEndpoint:
    """Tests for POST /workflows/v1/auth.

    Covers every AuthStatus, envelope format, credential passthrough,
    HandlerError, and unhandled exceptions — ensures the contract between
    the SDK and frontend never silently breaks again.
    """

    # -- Success ----------------------------------------------------------

    def test_auth_success_returns_200(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": [], "connection_id": "test-conn"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["status"] == "success"
        assert body["message"] == "auth ok"

    def test_auth_typed_failure_surfaces_the_error_message(self) -> None:
        client = _make_client(_AuthTypedFailureHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": [], "connection_id": "test-conn"},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["message"] == "The source rejected the credentials."
        assert body["data"]["message"] == "The source rejected the credentials."
        assert body["data"]["error"]["suggested_action"] == (
            "Check the username and password."
        )

    def test_auth_typed_failure_never_returns_the_exception_text(self) -> None:
        client = _make_client(_AuthCausedFailureHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": [], "connection_id": "test-conn"},
        )
        body = response.json()
        assert body["message"] == "The source rejected the credentials."
        assert "cause_repr" not in body["data"]["error"]
        assert "db.internal" not in response.text
        assert "svc_x" not in response.text

    def test_auth_success_envelope_has_all_fields(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        body = response.json()
        assert "success" in body
        assert "message" in body
        assert "data" in body
        # data must contain AuthOutput fields
        data = body["data"]
        assert "status" in data
        assert "identities" in data
        assert "scopes" in data

    def test_auth_with_credentials_succeeds(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "credentials": [{"key": "api_key", "value": "secret123"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["success"] is True

    # -- Failure (every non-SUCCESS AuthStatus) ---------------------------

    def test_auth_failed_returns_401(self) -> None:
        client = _make_client(handler=_AuthFailedHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["data"]["status"] == "failed"
        assert body["message"] == "bad credentials"

    def test_auth_expired_returns_401(self) -> None:
        client = _make_client(handler=_AuthExpiredHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["data"]["status"] == "expired"
        assert body["message"] == "token expired"

    def test_auth_invalid_credentials_returns_401(self) -> None:
        client = _make_client(handler=_AuthInvalidCredsHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["data"]["status"] == "invalid_credentials"
        assert body["message"] == "wrong key"

    # -- Failure envelope still has full structure ------------------------

    def test_auth_failure_envelope_matches_success_shape(self) -> None:
        """A failure response must have the same top-level keys as success
        so that consumers can parse it uniformly."""
        client = _make_client(handler=_AuthFailedHandler())
        body = client.post("/workflows/v1/auth", json={"credentials": []}).json()
        assert set(body.keys()) == {"success", "message", "data"}
        assert set(body["data"].keys()) >= {"status", "message", "identities", "scopes"}

    # -- Exception handling -----------------------------------------------

    def test_auth_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500
        assert "detail" in response.json()

    def test_auth_unhandled_exception_returns_500(self) -> None:
        """Unexpected exceptions (not HandlerError) must return 500 with a
        generic message — no stack traces leaked to the client."""
        client = _make_client(handler=_AuthUnhandledExceptionHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500
        body = response.json()
        assert body["detail"] == "Internal server error"
        assert "RuntimeError" not in str(body)

    def test_auth_source_unavailable_returns_503(self) -> None:
        """The category-to-status map serves every handler route, so the
        SOURCE_UNAVAILABLE entry added for /check applies here too."""
        client = _make_client(handler=_SourceDownHandler())
        response = client.post("/workflows/v1/auth", json={"credentials": []})
        assert response.status_code == 503
        assert response.json()["detail"] == "the warehouse did not answer"

    # -- AuthStatus.http_status / is_success properties -------------------

    def test_auth_status_http_codes(self) -> None:
        assert AuthStatus.SUCCESS.http_status == 200
        assert AuthStatus.FAILED.http_status == 401
        assert AuthStatus.EXPIRED.http_status == 401
        assert AuthStatus.INVALID_CREDENTIALS.http_status == 401

    def test_auth_status_is_success(self) -> None:
        assert AuthStatus.SUCCESS.is_success is True
        assert AuthStatus.FAILED.is_success is False
        assert AuthStatus.EXPIRED.is_success is False
        assert AuthStatus.INVALID_CREDENTIALS.is_success is False

    def test_every_auth_status_has_http_code(self) -> None:
        """If someone adds a new AuthStatus without updating the map, this
        test catches it."""
        for status in AuthStatus:
            assert isinstance(status.http_status, int), (
                f"{status} missing from _AUTH_STATUS_HTTP_CODES"
            )
