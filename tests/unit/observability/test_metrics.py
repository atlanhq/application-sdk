"""Unit tests for application_sdk.observability.metrics (user-facing OTel API)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from application_sdk.observability import metrics as _metrics_module


@pytest.fixture
def mock_meter():
    m = MagicMock()
    m.create_counter.return_value = MagicMock()
    m.create_histogram.return_value = MagicMock()
    m.create_up_down_counter.return_value = MagicMock()
    m.create_observable_counter.return_value = MagicMock()
    m.create_observable_up_down_counter.return_value = MagicMock()
    m.create_observable_gauge.return_value = MagicMock()
    with patch(
        "application_sdk.observability.metrics._otel_metrics.get_meter", return_value=m
    ):
        yield m


def test_meter_name_is_application_sdk_user(mock_meter):
    _metrics_module.create_counter("test.counter")
    mock_meter  # meter is constructed inside _meter() on each call
    # get_meter is patched at the module level; verify the name passed
    from unittest.mock import patch as _patch

    with _patch(
        "application_sdk.observability.metrics._otel_metrics.get_meter"
    ) as patched:
        patched.return_value = MagicMock()
        patched.return_value.create_counter.return_value = MagicMock()
        _metrics_module.create_counter("x")
        patched.assert_called_once_with("application_sdk.user")


def test_create_counter_forwards_name_unit_description(mock_meter):
    _metrics_module.create_counter("req.count", unit="1", description="Requests")
    mock_meter.create_counter.assert_called_once_with(
        name="req.count", unit="1", description="Requests"
    )


def test_create_counter_defaults(mock_meter):
    _metrics_module.create_counter("minimal")
    mock_meter.create_counter.assert_called_once_with(
        name="minimal", unit="", description=""
    )


def test_create_histogram_forwards_kwargs(mock_meter):
    _metrics_module.create_histogram("req.latency", unit="s", description="Latency")
    mock_meter.create_histogram.assert_called_once_with(
        name="req.latency", unit="s", description="Latency"
    )


def test_create_up_down_counter_forwards_kwargs(mock_meter):
    _metrics_module.create_up_down_counter(
        "active.conns", unit="1", description="Active connections"
    )
    mock_meter.create_up_down_counter.assert_called_once_with(
        name="active.conns", unit="1", description="Active connections"
    )


def test_create_observable_counter_passes_callbacks(mock_meter):
    cbs = [lambda: []]
    _metrics_module.create_observable_counter("obs.counter", cbs, unit="1")
    mock_meter.create_observable_counter.assert_called_once_with(
        name="obs.counter", callbacks=cbs, unit="1", description=""
    )


def test_create_observable_up_down_counter_passes_callbacks(mock_meter):
    cbs = [lambda: []]
    _metrics_module.create_observable_up_down_counter("obs.udc", cbs)
    mock_meter.create_observable_up_down_counter.assert_called_once_with(
        name="obs.udc", callbacks=cbs, unit="", description=""
    )


def test_create_observable_gauge_passes_callbacks(mock_meter):
    cbs = [lambda: []]
    _metrics_module.create_observable_gauge("obs.gauge", cbs, description="A gauge")
    mock_meter.create_observable_gauge.assert_called_once_with(
        name="obs.gauge", callbacks=cbs, unit="", description="A gauge"
    )


def test_counter_is_usable(mock_meter):
    counter = _metrics_module.create_counter("usable.counter")
    counter.add(1, {"k": "v"})
    counter.add.assert_called_once_with(1, {"k": "v"})
