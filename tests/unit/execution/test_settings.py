"""Unit tests for execution settings."""

from __future__ import annotations

from unittest import mock

import pytest
from temporalio.common import VersioningBehavior

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

    def test_default_versioning_behavior_is_pinned(self) -> None:
        settings = ExecutionSettings()
        assert settings.default_versioning_behavior == VersioningBehavior.PINNED

    def test_default_worker_tuner_mode_is_fixed(self) -> None:
        settings = ExecutionSettings()
        assert settings.worker_tuner_mode == "fixed"

    def test_default_tuner_targets(self) -> None:
        settings = ExecutionSettings()
        assert settings.tuner_target_memory_usage == 0.8
        assert settings.tuner_target_cpu_usage == 0.9

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

    def test_versioning_behavior_auto_upgrade_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", "AUTO_UPGRADE")
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.AUTO_UPGRADE

    def test_versioning_behavior_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", "auto_upgrade")
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.AUTO_UPGRADE

    def test_versioning_behavior_pinned_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", "PINNED")
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.PINNED

    def test_versioning_behavior_unknown_falls_back_to_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", "bogus")
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.PINNED

    def test_versioning_behavior_unset_defaults_to_pinned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR", raising=False)
        settings = load_execution_settings()
        assert settings.default_versioning_behavior == VersioningBehavior.PINNED


class TestLoadWorkerTunerSettingsFromEnv:
    """Tests for the worker tuner env vars (ATLAN_WORKER_TUNER_*)."""

    def test_tuner_mode_resource_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_MODE", "resource")
        settings = load_execution_settings()
        assert settings.worker_tuner_mode == "resource"

    def test_tuner_mode_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_MODE", "RESOURCE")
        settings = load_execution_settings()
        assert settings.worker_tuner_mode == "resource"

    def test_tuner_mode_fixed_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_MODE", "fixed")
        settings = load_execution_settings()
        assert settings.worker_tuner_mode == "fixed"

    def test_tuner_mode_unknown_falls_back_to_fixed_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_MODE", "bogus")
        with mock.patch("application_sdk.execution.settings.logger") as mock_logger:
            settings = load_execution_settings()
        assert settings.worker_tuner_mode == "fixed"
        assert mock_logger.warning.called

    def test_tuner_mode_unset_defaults_to_fixed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_WORKER_TUNER_MODE", raising=False)
        settings = load_execution_settings()
        assert settings.worker_tuner_mode == "fixed"

    def test_tuner_targets_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_TARGET_MEMORY", "0.5")
        monkeypatch.setenv("ATLAN_WORKER_TUNER_TARGET_CPU", "0.65")
        settings = load_execution_settings()
        assert settings.tuner_target_memory_usage == 0.5
        assert settings.tuner_target_cpu_usage == 0.65

    def test_tuner_target_one_is_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_TARGET_MEMORY", "1.0")
        settings = load_execution_settings()
        assert settings.tuner_target_memory_usage == 1.0

    def test_tuner_target_not_a_float_falls_back_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_TARGET_MEMORY", "lots")
        with mock.patch("application_sdk.execution.settings.logger") as mock_logger:
            settings = load_execution_settings()
        assert settings.tuner_target_memory_usage == 0.8
        assert mock_logger.warning.called

    def test_tuner_target_above_one_falls_back_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_TARGET_CPU", "1.5")
        with mock.patch("application_sdk.execution.settings.logger") as mock_logger:
            settings = load_execution_settings()
        assert settings.tuner_target_cpu_usage == 0.9
        assert mock_logger.warning.called

    def test_tuner_target_zero_falls_back_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_TARGET_CPU", "0")
        with mock.patch("application_sdk.execution.settings.logger") as mock_logger:
            settings = load_execution_settings()
        assert settings.tuner_target_cpu_usage == 0.9
        assert mock_logger.warning.called

    def test_tuner_target_negative_falls_back_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_TUNER_TARGET_MEMORY", "-0.4")
        with mock.patch("application_sdk.execution.settings.logger") as mock_logger:
            settings = load_execution_settings()
        assert settings.tuner_target_memory_usage == 0.8
        assert mock_logger.warning.called

    def test_tuner_targets_unset_use_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_WORKER_TUNER_TARGET_MEMORY", raising=False)
        monkeypatch.delenv("ATLAN_WORKER_TUNER_TARGET_CPU", raising=False)
        settings = load_execution_settings()
        assert settings.tuner_target_memory_usage == 0.8
        assert settings.tuner_target_cpu_usage == 0.9


class TestInterceptorSettingsDefaults:
    """Tests for InterceptorSettings default values."""

    def test_event_interceptor_enabled_by_default(self) -> None:
        settings = InterceptorSettings()
        assert settings.enable_event_interceptor is True

    def test_cleanup_interceptor_disabled_by_default(self) -> None:
        # CleanupInterceptor is deprecated in v3; cleanup is via App.on_complete()
        settings = InterceptorSettings()
        assert settings.enable_cleanup_interceptor is False


class TestLoadInterceptorSettingsFromEnv:
    """Tests for load_interceptor_settings() reading from environment."""

    def test_disable_event_interceptor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "false")
        settings = load_interceptor_settings()
        assert settings.enable_event_interceptor is False

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
