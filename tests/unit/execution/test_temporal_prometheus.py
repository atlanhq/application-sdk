"""Unit tests for Temporal Prometheus metrics integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.runtime import Runtime, TelemetryConfig

import application_sdk.execution._temporal.backend as backend_module
from application_sdk.execution._temporal.backend import (
    _get_prometheus_runtime,
    create_temporal_client,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the module-level singleton before and after each test."""
    backend_module._prometheus_runtime = None
    with patch.object(backend_module, "Runtime") as mock_cls:
        mock_cls.return_value = MagicMock(spec=Runtime)
        yield mock_cls
    backend_module._prometheus_runtime = None


def test_get_prometheus_runtime_creates_singleton(_reset_singleton):
    """First call creates a Runtime; second call returns the same instance."""
    rt1 = _get_prometheus_runtime()
    rt2 = _get_prometheus_runtime()
    assert rt1 is rt2
    _reset_singleton.assert_called_once()


def test_get_prometheus_runtime_skips_prometheus_when_disabled(_reset_singleton):
    """Prometheus-disabled runtimes should not bind a metrics listener."""
    with patch.object(backend_module, "ENABLE_PROMETHEUS_METRICS", False):
        runtime = _get_prometheus_runtime()

    assert runtime is backend_module._prometheus_runtime
    _reset_singleton.assert_called_once()
    telemetry = _reset_singleton.call_args.kwargs["telemetry"]
    assert isinstance(telemetry, TelemetryConfig)
    assert telemetry.metrics is None


def test_get_prometheus_runtime_enables_prometheus_when_configured(_reset_singleton):
    """Prometheus-enabled runtimes should bind the configured metrics address."""
    bind_address = "127.0.0.1:12345"

    with (
        patch.object(backend_module, "ENABLE_PROMETHEUS_METRICS", True),
        patch.object(backend_module, "TEMPORAL_PROMETHEUS_BIND_ADDRESS", bind_address),
    ):
        runtime = _get_prometheus_runtime()

    assert runtime is backend_module._prometheus_runtime
    _reset_singleton.assert_called_once()
    telemetry = _reset_singleton.call_args.kwargs["telemetry"]
    assert isinstance(telemetry, TelemetryConfig)
    assert telemetry.metrics is not None
    assert telemetry.metrics.bind_address == bind_address


@patch(
    "application_sdk.execution._temporal.backend.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_temporal_client_passes_runtime(mock_connect, _reset_singleton):
    """create_temporal_client passes a runtime kwarg to Client.connect."""
    mock_connect.return_value = MagicMock()

    await create_temporal_client(
        host="localhost:7233",
        namespace="default",
        connect_max_attempts=1,
    )

    mock_connect.assert_called_once()
    call_kwargs = mock_connect.call_args[1]
    assert "runtime" in call_kwargs
    assert call_kwargs["runtime"] is backend_module._prometheus_runtime
