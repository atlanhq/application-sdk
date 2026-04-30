"""Unit tests for Temporal Prometheus metrics integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.runtime import Runtime

import application_sdk.execution._temporal.backend as backend_module
from application_sdk.execution._temporal.backend import (
    _get_or_create_runtime,
    create_temporal_client,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the module-level singletons before and after each test."""
    backend_module._prometheus_runtime = None
    backend_module._default_runtime = None
    with patch.object(backend_module, "Runtime") as mock_cls:
        mock_cls.return_value = MagicMock(spec=Runtime)
        mock_cls.default.return_value = MagicMock(spec=Runtime)
        yield mock_cls
    backend_module._prometheus_runtime = None
    backend_module._default_runtime = None


def test_get_or_create_runtime_creates_prometheus_singleton(_reset_singleton):
    """First call with enable_prometheus=True creates a Runtime; second returns same."""
    rt1 = _get_or_create_runtime(enable_prometheus=True)
    rt2 = _get_or_create_runtime(enable_prometheus=True)
    assert rt1 is rt2
    _reset_singleton.assert_called_once()


def test_get_or_create_runtime_disabled_uses_default(_reset_singleton):
    """enable_prometheus=False uses Runtime.default() instead of Prometheus."""
    rt = _get_or_create_runtime(enable_prometheus=False)
    assert rt is not None
    _reset_singleton.default.assert_called_once()


def test_get_or_create_runtime_disabled_is_singleton(_reset_singleton):
    """Disabled runtime is also a singleton."""
    rt1 = _get_or_create_runtime(enable_prometheus=False)
    rt2 = _get_or_create_runtime(enable_prometheus=False)
    assert rt1 is rt2
    _reset_singleton.default.assert_called_once()


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


@patch(
    "application_sdk.execution._temporal.backend.Client.connect",
    new_callable=AsyncMock,
)
async def test_create_temporal_client_disables_prometheus(
    mock_connect, _reset_singleton
):
    """enable_prometheus=False skips Prometheus runtime creation."""
    mock_connect.return_value = MagicMock()

    await create_temporal_client(
        host="localhost:7233",
        namespace="default",
        connect_max_attempts=1,
        enable_prometheus=False,
    )

    mock_connect.assert_called_once()
    call_kwargs = mock_connect.call_args[1]
    assert "runtime" in call_kwargs
    # Should have used Runtime.default(), not PrometheusConfig
    _reset_singleton.default.assert_called_once()
