"""Unit tests for execution settings."""

from __future__ import annotations

import pytest

from application_sdk.execution.settings import (
    ExecutionSettings,
    InterceptorSettings,
    load_execution_settings,
    load_interceptor_settings,
)


class TestExecutionSettingsDefaults:
    """Tests for ExecutionSettings default values."""

    def test_default_host(self) -> None:
        settings = ExecutionSettings()
        assert settings.host == "localhost:7233"

    def test_default_namespace(self) -> None:
        settings = ExecutionSettings()
        assert settings.namespace == "default"

    def test_default_task_queue(self) -> None:
        settings = ExecutionSettings()
        assert settings.task_queue == "application-sdk"

    def test_default_max_concurrent_activities(self) -> None:
        settings = ExecutionSettings()
        assert settings.max_concurrent_activities == 100

    def test_default_graceful_shutdown_timeout(self) -> None:
        settings = ExecutionSettings()
        assert settings.graceful_shutdown_timeout_seconds == 3600

    def test_is_frozen(self) -> None:
        settings = ExecutionSettings()
        with pytest.raises(Exception):
            settings.host = "other:7233"  # type: ignore[misc]


class TestLoadExecutionSettingsFromEnv:
    """Tests for load_execution_settings() reading from environment."""

    def test_host_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_HOST", "temporal.example.com:7233")
        settings = load_execution_settings()
        assert settings.host == "temporal.example.com:7233"

    def test_namespace_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_NAMESPACE", "production")
        settings = load_execution_settings()
        assert settings.namespace == "production"

    def test_task_queue_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "my-custom-queue")
        settings = load_execution_settings()
        assert settings.task_queue == "my-custom-queue"

    def test_max_concurrent_activities_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "50")
        settings = load_execution_settings()
        assert settings.max_concurrent_activities == 50

    def test_graceful_shutdown_timeout_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", "7200")
        settings = load_execution_settings()
        assert settings.graceful_shutdown_timeout_seconds == 7200

    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in [
            "TEMPORAL_HOST",
            "TEMPORAL_NAMESPACE",
            "TEMPORAL_TASK_QUEUE",
            "TEMPORAL_MAX_CONCURRENT_ACTIVITIES",
            "TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT",
        ]:
            monkeypatch.delenv(key, raising=False)
        settings = load_execution_settings()
        assert settings.host == "localhost:7233"
        assert settings.namespace == "default"


class TestInterceptorSettingsDefaults:
    """Tests for InterceptorSettings default values."""

    def test_event_interceptor_enabled_by_default(self) -> None:
        settings = InterceptorSettings()
        assert settings.enable_event_interceptor is True

    def test_correlation_interceptor_enabled_by_default(self) -> None:
        settings = InterceptorSettings()
        assert settings.enable_correlation_interceptor is True

    def test_cleanup_interceptor_enabled_by_default(self) -> None:
        settings = InterceptorSettings()
        assert settings.enable_cleanup_interceptor is True


class TestLoadInterceptorSettingsFromEnv:
    """Tests for load_interceptor_settings() reading from environment."""

    def test_disable_event_interceptor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "false")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is False

    def test_disable_correlation_interceptor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CORRELATION_INTERCEPTOR", "false")
        settings = load_interceptor_settings()
        assert settings.enable_correlation_interceptor is False

    def test_disable_cleanup_interceptor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "false")
        settings = load_interceptor_settings()
        assert settings.enable_cleanup_interceptor is False

    def test_enable_via_true_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "true")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is True

    def test_enable_via_yes_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "yes")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is True

    def test_enable_via_one_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "1")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is True

    def test_disable_via_zero_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "0")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is False

    def test_disable_via_no_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "no")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is False

    def test_empty_string_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "")
        settings = load_interceptor_settings()
        # Empty string → default = True
        assert settings.enable_event_interceptor is True

    def test_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", raising=False)
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is True
