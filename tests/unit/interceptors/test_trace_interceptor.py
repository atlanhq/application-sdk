"""Unit tests for the TraceInterceptor."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from application_sdk import constants
from application_sdk.execution._temporal.interceptors.trace import TraceInterceptor


@pytest.fixture(autouse=True)
def traces_off(monkeypatch):
    monkeypatch.setattr(constants, "ENABLE_OTLP_TRACES", False)


@pytest.fixture
def setup_provider():
    """Stand-in for the tracer provider bootstrap, so no test installs a
    process-wide provider."""
    with patch(
        "application_sdk.observability.traces_adaptor.setup_tracer_provider"
    ) as mock_setup:
        yield mock_setup


@pytest.fixture
def enable_traces(monkeypatch, setup_provider):
    monkeypatch.setattr(constants, "ENABLE_OTLP_TRACES", True)
    return setup_provider


class TestTracesFlag:
    def test_reads_the_constant_not_the_env(self, monkeypatch, setup_provider):
        """FND-4642: the flag is read once, in constants. Setting the env var
        after import does not change the interceptor's decision."""
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_TRACES", "true")
        interceptor = TraceInterceptor()
        assert interceptor._delegate is None
        setup_provider.assert_not_called()

    def test_enabled_installs_the_tracer_provider(self, enable_traces):
        """FND-4642: the upstream interceptor emits through the global
        provider; without one installed, enabled tracing exported nothing."""
        mock_tracing_cls = MagicMock()
        with patch.dict(
            sys.modules,
            {
                "temporalio.contrib.opentelemetry": MagicMock(
                    TracingInterceptor=mock_tracing_cls
                )
            },
        ):
            TraceInterceptor()
        enable_traces.assert_called_once_with()


class TestTraceInterceptorDisabled:
    def test_delegate_is_none_when_disabled(self, setup_provider):
        interceptor = TraceInterceptor()
        setup_provider.assert_not_called()
        assert interceptor._delegate is None

    def test_workflow_interceptor_class_returns_none(self):
        interceptor = TraceInterceptor()
        result = interceptor.workflow_interceptor_class(MagicMock())
        assert result is None

    def test_intercept_activity_returns_next_unchanged(self):
        interceptor = TraceInterceptor()
        mock_next = MagicMock()
        result = interceptor.intercept_activity(mock_next)
        assert result is mock_next


class TestTraceInterceptorEnabled:
    def test_delegate_set_when_enabled(self, enable_traces):
        mock_tracing_cls = MagicMock()
        mock_instance = MagicMock()
        mock_tracing_cls.return_value = mock_instance

        with patch.dict(
            sys.modules,
            {
                "temporalio.contrib.opentelemetry": MagicMock(
                    TracingInterceptor=mock_tracing_cls
                )
            },
        ):
            interceptor = TraceInterceptor()

        assert interceptor._delegate is mock_instance

    def test_workflow_class_delegates_to_upstream(self, enable_traces):
        mock_tracing_cls = MagicMock()
        mock_instance = MagicMock()
        mock_tracing_cls.return_value = mock_instance
        expected_class = MagicMock()
        mock_instance.workflow_interceptor_class.return_value = expected_class

        with patch.dict(
            sys.modules,
            {
                "temporalio.contrib.opentelemetry": MagicMock(
                    TracingInterceptor=mock_tracing_cls
                )
            },
        ):
            interceptor = TraceInterceptor()

        inp = MagicMock()
        result = interceptor.workflow_interceptor_class(inp)
        mock_instance.workflow_interceptor_class.assert_called_once_with(inp)
        assert result is expected_class

    def test_intercept_activity_delegates_to_upstream(self, enable_traces):
        mock_tracing_cls = MagicMock()
        mock_instance = MagicMock()
        mock_tracing_cls.return_value = mock_instance
        expected_interceptor = MagicMock()
        mock_instance.intercept_activity.return_value = expected_interceptor

        with patch.dict(
            sys.modules,
            {
                "temporalio.contrib.opentelemetry": MagicMock(
                    TracingInterceptor=mock_tracing_cls
                )
            },
        ):
            interceptor = TraceInterceptor()

        mock_next = MagicMock()
        result = interceptor.intercept_activity(mock_next)
        mock_instance.intercept_activity.assert_called_once_with(mock_next)
        assert result is expected_interceptor


class TestTraceInterceptorImportError:
    def test_graceful_fallback_on_import_error(self, enable_traces):
        # Removing the module from sys.modules forces a fresh import that we
        # can make fail by temporarily replacing it with None.
        original = sys.modules.pop("temporalio.contrib.opentelemetry", None)
        try:
            with patch.dict(sys.modules, {"temporalio.contrib.opentelemetry": None}):
                interceptor = TraceInterceptor()
            assert interceptor._delegate is None
        finally:
            if original is not None:
                sys.modules["temporalio.contrib.opentelemetry"] = original
