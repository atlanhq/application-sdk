"""Unit tests for the TraceInterceptor."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.execution._temporal.interceptors.trace import (
    TraceInterceptor,
    _traces_enabled,
)


@pytest.fixture(autouse=True)
def unset_trace_env(monkeypatch):
    monkeypatch.delenv("ATLAN_ENABLE_OTLP_TRACES", raising=False)


class TestTracesEnabled:
    def test_default_is_false(self):
        assert _traces_enabled() is False

    def test_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_TRACES", "true")
        assert _traces_enabled() is True

    def test_false_when_env_set_to_false(self, monkeypatch):
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_TRACES", "false")
        assert _traces_enabled() is False

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_TRACES", "TRUE")
        assert _traces_enabled() is True


class TestTraceInterceptorDisabled:
    def test_delegate_is_none_when_disabled(self):
        interceptor = TraceInterceptor()
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
    def test_delegate_set_when_enabled(self, monkeypatch):
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_TRACES", "true")
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

    def test_workflow_class_delegates_to_upstream(self, monkeypatch):
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_TRACES", "true")
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

    def test_intercept_activity_delegates_to_upstream(self, monkeypatch):
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_TRACES", "true")
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
    def test_graceful_fallback_on_import_error(self, monkeypatch):
        monkeypatch.setenv("ATLAN_ENABLE_OTLP_TRACES", "true")
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
