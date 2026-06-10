"""Unit tests for SecurityHeadersMiddleware."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application_sdk.server.middleware import SecurityHeadersMiddleware


class TestSecurityHeadersMiddleware:
    def _client(self) -> TestClient:
        app = FastAPI()
        app.add_middleware(SecurityHeadersMiddleware)

        @app.get("/ok")
        def ok() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/custom-frame")
        def custom_frame():  # type: ignore[no-untyped-def]
            from fastapi.responses import JSONResponse

            return JSONResponse(content={}, headers={"X-Frame-Options": "SAMEORIGIN"})

        return TestClient(app)

    def test_headers_present_on_responses(self) -> None:
        response = self._client().get("/ok")
        assert response.status_code == 200
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"

    def test_headers_present_on_404(self) -> None:
        response = self._client().get("/missing")
        assert response.status_code == 404
        assert response.headers["x-frame-options"] == "DENY"

    def test_existing_header_not_overwritten(self) -> None:
        response = self._client().get("/custom-frame")
        assert response.headers["x-frame-options"] == "SAMEORIGIN"
        # The other headers are still added.
        assert response.headers["x-content-type-options"] == "nosniff"
