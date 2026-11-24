"""The handler service emits the documented stable-semconv HTTP metric.

``docs/standards/metrics.md`` and the PromQL across the docs query
``http_server_request_duration_seconds``. FastAPIInstrumentor only emits that
name (``http.server.request.duration``) when ``OTEL_SEMCONV_STABILITY_OPT_IN``
includes ``http``; otherwise it emits the legacy ``http.server.duration``
(milliseconds). The SDK sets that default itself, so the contract holds on
runtimes whose environment does not.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation import _semconv
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from application_sdk.handler.service import create_app_handler_service
from tests.unit.handler._service_support import _TestHandler

_OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"


@pytest.fixture
def metric_reader(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryMetricReader]:
    """Route the instrumentor's meter to an in-memory reader, with the opt-in
    read fresh.

    The instrumentation library caches the opt-in once per process; reset the
    cache so this test sees what ``create_app_handler_service`` sets, not
    whatever an earlier test in the session left behind.
    """
    stability = _semconv._OpenTelemetrySemanticConventionStability
    monkeypatch.setattr(stability, "_initialized", False)
    monkeypatch.setattr(stability, "_OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING", {})

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    real_instrument_app = FastAPIInstrumentor.instrument_app

    def _instrument_with_local_provider(
        app: FastAPI, excluded_urls: str | None = None
    ) -> None:
        real_instrument_app(app, excluded_urls=excluded_urls, meter_provider=provider)

    monkeypatch.setattr(
        FastAPIInstrumentor,
        "instrument_app",
        staticmethod(_instrument_with_local_provider),
    )
    yield reader
    provider.shutdown()


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        metric.name
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


def _serve_one_request(tmp_path: Path) -> None:
    app = create_app_handler_service(
        _TestHandler(),
        app_name="semconv-test",
        frontend_assets_path=str(tmp_path),
    )
    TestClient(app).get("/")


def test_stable_http_duration_metric_with_opt_in_unset(
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(_OPT_IN, raising=False)

    _serve_one_request(tmp_path)

    names = _metric_names(metric_reader)
    assert "http.server.request.duration" in names
    assert "http.server.duration" not in names


def test_explicit_opt_in_is_not_overridden(
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # http/dup emits both the legacy and the stable names; the SDK default
    # ("http") would emit only the stable one. Seeing both proves the
    # operator's value won.
    monkeypatch.setenv(_OPT_IN, "http/dup")

    _serve_one_request(tmp_path)

    names = _metric_names(metric_reader)
    assert {"http.server.request.duration", "http.server.duration"} <= names
