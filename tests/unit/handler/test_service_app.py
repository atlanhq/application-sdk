"""Handler service tests: service assembly: frontend, metrics, run helper, client config, telemetry and boundary-log redaction."""

from __future__ import annotations

import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
)
from application_sdk.handler.service import (
    create_app_handler_service,
)
from tests.unit.handler._service_support import (
    _make_client,
    _TestHandler,
)


class TestFrontendHomeEndpoint:
    """Tests for GET / (frontend index.html)."""

    def test_frontend_home_missing_returns_placeholder_html(
        self, tmp_path: Path
    ) -> None:
        # frontend_assets_path doesn't contain index.html
        app = create_app_handler_service(
            _TestHandler(),
            app_name="ui-test",
            frontend_assets_path=str(tmp_path / "missing"),
        )
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 404
        assert "UI not available" in response.text

    def test_frontend_home_serves_index_html_when_present(self, tmp_path: Path) -> None:
        assets = tmp_path / "static"
        assets.mkdir()
        (assets / "index.html").write_text("<html><body>HELLO</body></html>")
        app = create_app_handler_service(
            _TestHandler(),
            app_name="ui-test-2",
            frontend_assets_path=str(assets),
        )
        client = TestClient(app)
        response = client.get("/")
        assert response.status_code == 200
        assert "HELLO" in response.text


class TestMetricsEndpoint:
    """Tests for GET /metrics Temporal-core proxy behavior."""

    def test_metrics_skips_temporal_core_proxy_when_disabled(
        self, tmp_path: Path
    ) -> None:
        app = create_app_handler_service(
            _TestHandler(),
            app_name="metrics-test",
            enable_temporal_core_metrics=False,
            frontend_assets_path=str(tmp_path),
        )
        client = TestClient(app)

        with patch("httpx.AsyncClient") as mock_async_client:
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "python_info" in response.text
        mock_async_client.assert_not_called()

    @staticmethod
    def _core_transport(handler):
        """Route the proxy's ``httpx.AsyncClient`` through a MockTransport."""
        import httpx

        real_async_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)

        def _client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return real_async_client(*args, transport=transport, **kwargs)  # type: ignore[arg-type]

        return patch("httpx.AsyncClient", side_effect=_client)

    @staticmethod
    def _proxying_client(tmp_path: Path) -> TestClient:
        app = create_app_handler_service(
            _TestHandler(),
            app_name="metrics-test",
            enable_temporal_core_metrics=True,
            prometheus_bind_address="127.0.0.1:9464",
            frontend_assets_path=str(tmp_path),
        )
        return TestClient(app)

    def test_metrics_merges_temporal_core_series(self, tmp_path: Path) -> None:
        import httpx

        core = b'temporal_num_pollers{poller_type="workflow_task"} 2.0\n'
        client = self._proxying_client(tmp_path)

        with (
            self._core_transport(lambda _r: httpx.Response(200, content=core)),
            patch("application_sdk.handler.service._record_proxy_failure") as failure,
        ):
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "python_info" in response.text
        assert 'temporal_num_pollers{poller_type="workflow_task"} 2.0' in response.text
        failure.assert_not_called()

    @pytest.mark.parametrize("declared_length", [True, False])
    def test_metrics_oversize_core_body_is_dropped_and_counted(
        self, tmp_path: Path, declared_length: bool
    ) -> None:
        """An oversize core exposition is never read into handler memory whole.

        Covers both a declared ``Content-Length`` and a chunked body with no
        length, where the running byte total must trip the cap mid-stream.
        """
        import httpx

        cap = 1024
        pulled = 0

        class _Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal pulled
                while pulled < cap * 100:
                    pulled += 64
                    yield b"#" * 64

        def _serve(_request: httpx.Request) -> httpx.Response:
            if declared_length:
                return httpx.Response(200, content=b"#" * (cap + 1))
            return httpx.Response(200, stream=_Chunks())

        client = self._proxying_client(tmp_path)
        with (
            patch("application_sdk.constants.TEMPORAL_CORE_METRICS_MAX_BYTES", cap),
            self._core_transport(_serve),
            patch("application_sdk.handler.service._record_proxy_failure") as failure,
        ):
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "python_info" in response.text
        assert "#" * 64 not in response.text
        failure.assert_called_once()
        assert failure.call_args.args[0] == "oversize"
        if not declared_length:
            assert pulled < cap * 3

    def test_metrics_non_200_is_counted_with_status_reason(
        self, tmp_path: Path
    ) -> None:
        import httpx

        client = self._proxying_client(tmp_path)
        with (
            self._core_transport(
                lambda _r: httpx.Response(
                    503, content=b"temporal_core_503_body_marker 1\n"
                )
            ),
            patch("application_sdk.handler.service._record_proxy_failure") as failure,
        ):
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "temporal_core_503_body_marker" not in response.text
        failure.assert_called_once()
        assert failure.call_args.args[0] == "http_503"

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param("connect", id="connect_error"),
            pytest.param("timeout", id="read_timeout"),
        ],
    )
    def test_metrics_request_error_logs_warning_with_traceback(
        self, tmp_path: Path, error: str
    ) -> None:
        import httpx

        def _fail(request: httpx.Request) -> httpx.Response:
            if error == "connect":
                raise httpx.ConnectError(
                    "All connection attempts failed", request=request
                )
            raise httpx.ReadTimeout("read timed out", request=request)

        client = self._proxying_client(tmp_path)
        with (
            self._core_transport(_fail),
            patch("application_sdk.handler.service.logger.debug") as mock_debug,
            patch("application_sdk.handler.service.logger.warning") as mock_warning,
        ):
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "python_info" in response.text
        mock_debug.assert_not_called()
        mock_warning.assert_called_once()
        assert "proxy request failed" in mock_warning.call_args.args[0]
        assert mock_warning.call_args.kwargs.get("exc_info") is True


class TestRunAppHandlerService:
    """Smoke test for run_app_handler_service — ensures uvicorn wiring is OK."""

    def test_run_invokes_uvicorn_with_built_app(self) -> None:
        from unittest.mock import patch

        from application_sdk.handler.service import run_app_handler_service

        with patch("uvicorn.run") as mock_run:
            run_app_handler_service(
                _TestHandler(), host="127.0.0.1", port=9000, log_level="warning"
            )
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        # First positional arg is the FastAPI app instance
        assert kwargs.get("host") == "127.0.0.1"
        assert kwargs.get("port") == 9000
        assert kwargs.get("log_level") == "warning"


class TestWorkflowClientConfig:
    """Tests for WorkflowClientConfig.is_configured()."""

    def test_unconfigured_when_host_missing(self) -> None:
        from application_sdk.handler.service import WorkflowClientConfig

        cfg = WorkflowClientConfig(host="", task_queue="q")
        assert cfg.is_configured() is False

    def test_unconfigured_when_app_class_missing(self) -> None:
        from application_sdk.handler.service import WorkflowClientConfig

        cfg = WorkflowClientConfig(host="t:7233", app_class=None)
        assert cfg.is_configured() is False

    def test_configured_when_host_and_app_class_present(self) -> None:
        from application_sdk.handler.service import WorkflowClientConfig

        class _Cls:
            pass

        cfg = WorkflowClientConfig(host="t:7233", app_class=_Cls, app_name="test-app")  # type: ignore[arg-type]
        assert cfg.is_configured() is True


class TestStateStoreDeprecation:
    """state_store= parameter is deprecated and ignored — verify warning."""

    def test_state_store_emits_deprecation_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            create_app_handler_service(
                _TestHandler(),
                app_name="dep-test",
                state_store="some-state-store",
            )
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "state_store" in str(w.message)
            for w in caught
        )

    def test_no_state_store_no_deprecation_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            create_app_handler_service(_TestHandler(), app_name="no-dep")
        # No state_store warning should be emitted
        assert not any(
            issubclass(w.category, DeprecationWarning)
            and "state_store" in str(w.message)
            for w in caught
        )


# ---------------------------------------------------------------------------
# Boundary logs must not carry credentials the driver quoted
# ---------------------------------------------------------------------------

_LEAKY_SECRET = "hunter2-not-a-real-secret"
_LEAKY_TEXT = (
    f"connect failed: mongodb://svc:{_LEAKY_SECRET}@db.internal/"
    f"?tlsCertificateKeyFilePassword={_LEAKY_SECRET}"
)


class _LeakyHandler(Handler):
    """Every route fails with a driver error that quotes a connection URI.

    ``chained`` wraps it in a HandlerError (the deprecated typed path);
    otherwise the raw driver error escapes (the unexpected-exception path).
    """

    def __init__(self, chained: bool) -> None:
        super().__init__()
        self._chained = chained

    def _fail(self) -> Exception:
        driver = RuntimeError(_LEAKY_TEXT)
        if not self._chained:
            return driver
        try:
            raise driver
        except RuntimeError as exc:
            err = HandlerError("source failed")
            err.__cause__ = exc
            return err

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise self._fail()

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise self._fail()

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        raise self._fail()


class TestBoundaryLogRedaction:
    """The HTTP boundary is the only server-side record of a handler failure,
    so it keeps the traceback — but through the SDK redaction helpers, never
    as a raw ``exc_info`` that serialises the driver's message verbatim."""

    @pytest.mark.parametrize("chained", [True, False], ids=["handler-error", "raw"])
    @pytest.mark.parametrize(
        "route", ["/workflows/v1/auth", "/workflows/v1/check", "/workflows/v1/metadata"]
    )
    def test_boundary_log_redacts_message_and_traceback(
        self, route: str, chained: bool
    ) -> None:
        client = _make_client(handler=_LeakyHandler(chained=chained))
        with patch("application_sdk.handler.routes.logger") as ml:
            response = client.post(route, json={"credentials": []})
        assert response.status_code >= 500
        assert _LEAKY_SECRET not in response.text

        boundary = [
            c
            for c in ml.error.call_args_list
            if c.args and isinstance(c.args[0], str) and "(request %s)" in c.args[0]
        ]
        assert boundary, f"no boundary log for {route}"
        for call in boundary:
            assert "exc_info" not in call.kwargs
            rendered = call.args[0] % call.args[1:]
            assert _LEAKY_SECRET not in rendered
            assert "Traceback" in rendered  # the stack survives, redacted


class TestFastAPINativeTelemetry:
    """FastAPI >=0.142 auto-configures OTLP/HTTP export on the global
    providers when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. The SDK owns those
    providers, so the handler app must leave them untouched at startup."""

    def test_startup_leaves_global_tracer_provider_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opentelemetry import trace

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4317")
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)

        def _processors(provider: object) -> list[object]:
            active = getattr(provider, "_active_span_processor", None)
            return list(getattr(active, "_span_processors", ()))

        before = trace.get_tracer_provider()
        processors_before = _processors(before)

        with _make_client():
            pass

        after = trace.get_tracer_provider()
        assert after is before
        assert _processors(after) == processors_before
