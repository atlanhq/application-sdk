"""Unit tests for LogMiddleware path-exclusion behavior."""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application_sdk.server.middleware import EXCLUDED_LOG_PATHS, LogMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LogMiddleware)

    @app.get("/server/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/server/ready")
    def ready() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics() -> str:
        return "metrics"

    @app.get("/automation/server/health")
    def prefixed_health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/automation/server/ready")
    def prefixed_ready() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/automation/metrics")
    def prefixed_metrics() -> str:
        return "metrics"

    @app.get("/workflows/start")
    def workflows() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_build_app())


@pytest.mark.parametrize(
    "path",
    [
        "/server/health",
        "/server/ready",
        "/metrics",
        "/automation/server/health",
        "/automation/server/ready",
        "/automation/metrics",
    ],
)
def test_excluded_paths_are_not_logged(client: TestClient, path: str) -> None:
    """Excluded paths must not produce request-started or request-completed logs,
    regardless of whether the app is mounted under a prefix."""
    with patch("application_sdk.server.middleware.log.logger.info") as mock_info:
        response = client.get(path)
    assert response.status_code == 200
    assert (
        mock_info.call_count == 0
    ), f"path {path} unexpectedly produced log entries: {mock_info.call_args_list}"


def test_non_excluded_path_is_logged(client: TestClient) -> None:
    """Non-excluded paths emit both a started and a completed log entry."""
    with patch("application_sdk.server.middleware.log.logger.info") as mock_info:
        response = client.get("/workflows/start")
    assert response.status_code == 200
    # one "Request started", one "Request completed"
    assert mock_info.call_count == 2


def test_excluded_log_paths_set_contents() -> None:
    """Regression: ensure the canonical exclusion set hasn't drifted."""
    assert "/server/health" in EXCLUDED_LOG_PATHS
    assert "/server/ready" in EXCLUDED_LOG_PATHS
    assert "/metrics" in EXCLUDED_LOG_PATHS
    assert "/health" in EXCLUDED_LOG_PATHS
    assert "/ready" in EXCLUDED_LOG_PATHS
